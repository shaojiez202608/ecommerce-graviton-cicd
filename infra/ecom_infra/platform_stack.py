"""Per-environment runtime: VPC, RDS for MySQL, ElastiCache (Valkey), OpenSearch, EFS, ECS on
Fargate ARM64 (Graviton), internal ALB, monitoring, and the in-VPC deploy project used by the
pipeline. One stack per environment: <PROJECT>-<ENV>-platform.
"""

from __future__ import annotations

import json

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_efs as efs
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from .cicd_stack import artifact_bucket_name
from .config import SYNTH_CHECK_CERTIFICATE, AppConfig, EnvConfig

WEB_PORT = 8080
REDIS_PORT = 6379
DB_PORT = 3306
WWW_DATA_UID = "33"  # www-data in Debian-based official PHP images


class PlatformStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_cfg: AppConfig,
        env_cfg: EnvConfig,
        image_tag: str | None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.app_cfg = app_cfg
        self.env_cfg = env_cfg
        env = env_cfg.name
        removal = RemovalPolicy.DESTROY if app_cfg.destroy_on_delete else RemovalPolicy.RETAIN
        log_retention = logs.RetentionDays.ONE_WEEK if app_cfg.destroy_on_delete else logs.RetentionDays.THREE_MONTHS
        n = lambda *p: app_cfg.name(env, *p)  # noqa: E731

        # ------------------------------------------------------------------ network
        flow_logs = logs.LogGroup(
            self, "VpcFlowLogs", log_group_name=f"/{app_cfg.project}/{env}/vpc-flow", retention=log_retention,
            removal_policy=removal,
        )
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=n("vpc"),
            ip_addresses=ec2.IpAddresses.cidr(env_cfg.vpc_cidr),
            max_azs=env_cfg.max_azs,
            nat_gateways=env_cfg.nat_gateways,
            restrict_default_security_group=True,
            subnet_configuration=[
                # Public subnets host only the NAT gateways; nothing else is placed there.
                ec2.SubnetConfiguration(name="nat", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24,
                                        map_public_ip_on_launch=False),
                ec2.SubnetConfiguration(name="app", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22),
                ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
            flow_logs={
                "all": ec2.FlowLogOptions(destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs))
            },
        )
        app_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        data_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        sg = lambda name, desc: ec2.SecurityGroup(  # noqa: E731
            self, name, vpc=self.vpc, description=desc, allow_all_outbound=False,
            security_group_name=n(name.lower()),
        )
        self.alb_sg = sg("AlbSg", "Internal ALB; ingress only from the VPC and configured internal CIDRs")
        self.web_sg = sg("WebSg", "Magento web and one-off tasks")
        self.db_sg = sg("DbSg", "RDS for MySQL")
        self.cache_sg = sg("CacheSg", "ElastiCache Valkey")
        self.search_sg = sg("SearchSg", "OpenSearch domain")
        self.efs_sg = sg("EfsSg", "EFS shared media/var")
        self.deploy_sg = sg("DeploySg", "In-VPC CodeBuild deploy and smoke test")

        # HTTPS environments listen on 443 (80 only redirects); HTTP environments listen on 80.
        listener_ports = (80, 443) if env_cfg.https else (80,)
        self.site_port = 443 if env_cfg.https else 80
        for cidr in (env_cfg.vpc_cidr, *env_cfg.allowed_ingress_cidrs):
            for port in listener_ports:
                self.alb_sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(port), f"port {port} from {cidr}")
        self.alb_sg.add_egress_rule(self.web_sg, ec2.Port.tcp(WEB_PORT), "to web tasks")
        self.web_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(WEB_PORT), "from ALB")
        for target, port, label in (
            (self.db_sg, DB_PORT, "MySQL"),
            (self.cache_sg, REDIS_PORT, "Valkey"),
            (self.search_sg, 443, "OpenSearch"),
            (self.efs_sg, 2049, "NFS"),
        ):
            target.add_ingress_rule(self.web_sg, ec2.Port.tcp(port), f"{label} from web/one-off tasks")
            self.web_sg.add_egress_rule(target, ec2.Port.tcp(port), f"to {label}")
        # HTTPS egress for ECR/Secrets Manager/SSM/CloudWatch via NAT; Magento itself needs no other egress.
        self.web_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "AWS APIs over HTTPS via NAT")
        self.deploy_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "AWS APIs over HTTPS via NAT")
        self.deploy_sg.add_egress_rule(self.alb_sg, ec2.Port.tcp(self.site_port), "smoke test against internal ALB")
        self.alb_sg.add_ingress_rule(self.deploy_sg, ec2.Port.tcp(self.site_port), "smoke test from deploy project")

        # ------------------------------------------------------------------ alarms topic
        self.alarm_topic = sns.Topic(
            self, "AlarmTopic", topic_name=n("alarms"),
            master_key=None, enforce_ssl=True,
        )
        if app_cfg.build.alarm_email:
            self.alarm_topic.add_subscription(subs.EmailSubscription(app_cfg.build.alarm_email))

        # ------------------------------------------------------------------ secrets
        self.db_secret = rds.DatabaseSecret(self, "DbSecret", username="magento", secret_name=n("db"))
        # Application DB user with explicit grants on the Magento schema only. The migration task
        # creates/updates it with the master credentials; web tasks never see the master secret.
        self.db_app_secret = sm.Secret(
            self, "DbAppSecret", secret_name=n("db-app"),
            description="Magento application database user (least privilege on the magento schema)",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "magento_app"}),
                generate_string_key="password", exclude_punctuation=True, password_length=32,
            ),
            removal_policy=removal,
        )
        self.app_secret = sm.Secret(
            self,
            "AppSecret",
            secret_name=n("magento"),
            description="Magento crypt key and admin credentials (generated)",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps({"admin_user": "admin"}),
                generate_string_key="admin_password",
                exclude_punctuation=True,
                password_length=24,
            ),
            removal_policy=removal,
        )
        self.crypt_secret = sm.Secret(
            self, "CryptKey", secret_name=n("crypt-key"),
            description="Magento crypt key (32 chars)",
            generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=32),
            removal_policy=removal,
        )
        self.search_secret = sm.Secret(
            self, "SearchSecret", secret_name=n("opensearch"),
            description="OpenSearch fine-grained access control master user",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "magento"}),
                generate_string_key="password",
                password_length=24,
                require_each_included_type=True,
                exclude_characters="\"'\\/@ `",
            ),
            removal_policy=removal,
        )
        self.cache_secret = sm.Secret(
            self, "CacheAuth", secret_name=n("valkey-auth"),
            description="ElastiCache AUTH token",
            generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=32),
            removal_policy=removal,
        )

        # ------------------------------------------------------------------ database
        params = rds.ParameterGroup(
            self, "DbParams",
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.of(env_cfg.db_engine_version, ".".join(env_cfg.db_engine_version.split(".")[:2]))
            ),
            parameters={"require_secure_transport": "ON", "log_bin_trust_function_creators": "1"},
        )
        self.db = rds.DatabaseInstance(
            self,
            "Db",
            instance_identifier=n("mysql"),
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.of(env_cfg.db_engine_version, ".".join(env_cfg.db_engine_version.split(".")[:2]))
            ),
            instance_type=ec2.InstanceType(env_cfg.db_instance_class),
            vpc=self.vpc,
            vpc_subnets=data_subnets,
            security_groups=[self.db_sg],
            credentials=rds.Credentials.from_secret(self.db_secret),
            database_name="magento",
            parameter_group=params,
            multi_az=env_cfg.db_multi_az,
            storage_encrypted=True,
            allocated_storage=env_cfg.db_allocated_storage_gib,
            storage_type=rds.StorageType.GP3,
            backup_retention=Duration.days(1 if app_cfg.destroy_on_delete else 7),
            delete_automated_backups=app_cfg.destroy_on_delete,
            deletion_protection=not app_cfg.destroy_on_delete,
            removal_policy=removal if not app_cfg.destroy_on_delete else RemovalPolicy.DESTROY,
            cloudwatch_logs_exports=["error", "slowquery"],
            cloudwatch_logs_retention=log_retention,
            auto_minor_version_upgrade=True,
            publicly_accessible=False,
        )

        # ------------------------------------------------------------------ cache (Valkey)
        cache_subnets = elasticache.CfnSubnetGroup(
            self, "CacheSubnets",
            description=f"{n('valkey')} subnets",
            cache_subnet_group_name=n("valkey"),
            subnet_ids=self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
        )
        multi_node = env_cfg.db_multi_az
        self.cache = elasticache.CfnReplicationGroup(
            self,
            "Cache",
            replication_group_id=n("valkey"),
            replication_group_description=f"Magento cache and sessions ({env})",
            engine="valkey",
            engine_version=env_cfg.cache_engine_version,
            cache_node_type=env_cfg.cache_node_type,
            num_cache_clusters=2 if multi_node else 1,
            automatic_failover_enabled=multi_node,
            multi_az_enabled=multi_node,
            cache_subnet_group_name=cache_subnets.cache_subnet_group_name,
            security_group_ids=[self.cache_sg.security_group_id],
            at_rest_encryption_enabled=True,
            transit_encryption_enabled=True,
            transit_encryption_mode="required",
            auth_token=self.cache_secret.secret_value.unsafe_unwrap(),  # dynamic reference, not plaintext
            port=REDIS_PORT,
            snapshot_retention_limit=0 if app_cfg.destroy_on_delete else 1,
        )
        self.cache.node.add_dependency(cache_subnets)
        self.cache.apply_removal_policy(removal)

        # ------------------------------------------------------------------ search (OpenSearch)
        search_logs = logs.LogGroup(
            self, "SearchAppLogs", log_group_name=f"/{app_cfg.project}/{env}/opensearch-app",
            retention=log_retention, removal_policy=removal,
        )
        zone_aware = env_cfg.search_data_nodes > 1
        self.search = opensearch.Domain(
            self,
            "Search",
            domain_name=n("search"),
            version=opensearch.EngineVersion.open_search(env_cfg.search_engine_version.replace("OpenSearch_", "")),
            vpc=self.vpc,
            vpc_subnets=[ec2.SubnetSelection(
                subnets=self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets[
                    : (2 if zone_aware else 1)
                ]
            )],
            security_groups=[self.search_sg],
            capacity=opensearch.CapacityConfig(
                data_nodes=env_cfg.search_data_nodes,
                data_node_instance_type=env_cfg.search_instance_type,
                multi_az_with_standby_enabled=False,
            ),
            zone_awareness=opensearch.ZoneAwarenessConfig(enabled=zone_aware, availability_zone_count=2)
            if zone_aware else opensearch.ZoneAwarenessConfig(enabled=False),
            ebs=opensearch.EbsOptions(volume_size=env_cfg.search_volume_gib, volume_type=ec2.EbsDeviceVolumeType.GP3),
            encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
            node_to_node_encryption=True,
            enforce_https=True,
            tls_security_policy=opensearch.TLSSecurityPolicy.TLS_1_2,
            fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                master_user_name="magento",
                master_user_password=self.search_secret.secret_value_from_json("password"),
            ),
            logging=opensearch.LoggingOptions(app_log_enabled=True, app_log_group=search_logs),
            removal_policy=removal,
        )
        # Fine-grained access control authenticates every request; the resource policy allows the
        # domain to be reached from inside the VPC only (VPC endpoint + security group).
        self.search.add_access_policies(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["es:ESHttp*"],
                resources=[f"{self.search.domain_arn}/*"],
            )
        )

        # ------------------------------------------------------------------ shared files (EFS)
        self.fs = efs.FileSystem(
            self, "SharedFs",
            file_system_name=n("shared"),
            vpc=self.vpc,
            vpc_subnets=data_subnets,
            security_group=self.efs_sg,
            encrypted=True,
            removal_policy=removal,
            enable_automatic_backups=not app_cfg.destroy_on_delete,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.ELASTIC,
        )
        posix = efs.PosixUser(uid=WWW_DATA_UID, gid=WWW_DATA_UID)
        acl = efs.Acl(owner_uid=WWW_DATA_UID, owner_gid=WWW_DATA_UID, permissions="0755")
        media_ap = self.fs.add_access_point("MediaAp", path="/media", posix_user=posix, create_acl=acl)
        var_ap = self.fs.add_access_point("VarAp", path="/var", posix_user=posix, create_acl=acl)

        # ------------------------------------------------------------------ ECS
        self.cluster = ecs.Cluster(
            self, "Cluster", cluster_name=n("cluster"), vpc=self.vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        app_logs = logs.LogGroup(
            self, "AppLogs", log_group_name=f"/{app_cfg.project}/{env}/magento", retention=log_retention,
            removal_policy=removal,
        )
        repo = ecr.Repository.from_repository_name(self, "Repo", app_cfg.name("magento"))
        image_ref = image_tag or "bootstrap"
        self.image_tag = image_ref
        ssm_base_url = ssm.StringParameter(
            self, "BaseUrlParam", parameter_name=app_cfg.ssm(env, "base_url"),
            string_value="http://pending.invalid/",  # overwritten below once the ALB exists
            description="Magento base URL injected at runtime",
        )
        ssm_admin = ssm.StringParameter(
            self, "AdminPathParam", parameter_name=app_cfg.ssm(env, "admin_frontname"),
            string_value=f"admin_{env}", description="Magento admin front name injected at runtime",
        )

        def task_definition(construct_id: str, family: str, cpu: int, mem: int, command: list[str] | None):
            td = ecs.FargateTaskDefinition(
                self, construct_id, family=family, cpu=cpu, memory_limit_mib=mem,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.ARM64,
                    operating_system_family=ecs.OperatingSystemFamily.LINUX,
                ),
            )
            for ap, vol in ((media_ap, "media"), (var_ap, "var")):
                td.add_volume(
                    name=vol,
                    efs_volume_configuration=ecs.EfsVolumeConfiguration(
                        file_system_id=self.fs.file_system_id,
                        transit_encryption="ENABLED",
                        authorization_config=ecs.AuthorizationConfig(access_point_id=ap.access_point_id, iam="ENABLED"),
                    ),
                )
            self.fs.grant_read_write(td.task_role)
            container = td.add_container(
                "web",
                container_name="web",
                # init process forwards SIGTERM (ECS stop / deploy timeout) to the scripts so their
                # traps run; one-off tasks get the longest stop timeout Fargate allows.
                linux_parameters=ecs.LinuxParameters(self, f"{construct_id}Linux", init_process_enabled=True),
                stop_timeout=Duration.seconds(120) if command is not None else None,
                image=ecs.ContainerImage.from_ecr_repository(repo, image_ref),
                command=command,
                entry_point=["/usr/local/bin/magento-entrypoint"] if command is None else ["/bin/sh", "-c"],
                logging=ecs.LogDriver.aws_logs(stream_prefix=family, log_group=app_logs),
                port_mappings=[ecs.PortMapping(container_port=WEB_PORT)] if command is None else None,
                environment={
                    "APP_ENV": env,
                    "MAGENTO_DB_HOST": self.db.db_instance_endpoint_address,
                    "MAGENTO_DB_NAME": "magento",
                    "MAGENTO_REDIS_HOST": self.cache.attr_primary_end_point_address,
                    "MAGENTO_REDIS_PORT": str(REDIS_PORT),
                    "MAGENTO_REDIS_TLS": "1",
                    "MAGENTO_OPENSEARCH_HOST": self.search.domain_endpoint,
                    "MAGENTO_OPENSEARCH_PREFIX": f"magento_{env}",
                    "MAGENTO_CACHE_PREFIX": f"{env}_",
                },
                secrets={
                    "MAGENTO_DB_USER": ecs.Secret.from_secrets_manager(self.db_app_secret, "username"),
                    "MAGENTO_DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db_app_secret, "password"),
                    "MAGENTO_CRYPT_KEY": ecs.Secret.from_secrets_manager(self.crypt_secret),
                    "MAGENTO_OPENSEARCH_USER": ecs.Secret.from_secrets_manager(self.search_secret, "username"),
                    "MAGENTO_OPENSEARCH_PASSWORD": ecs.Secret.from_secrets_manager(self.search_secret, "password"),
                    "MAGENTO_REDIS_PASSWORD": ecs.Secret.from_secrets_manager(self.cache_secret),
                    "MAGENTO_BASE_URL": ecs.Secret.from_ssm_parameter(ssm_base_url),
                    "MAGENTO_ADMIN_FRONTNAME": ecs.Secret.from_ssm_parameter(ssm_admin),
                    **(
                        {
                            "MAGENTO_DB_MASTER_USER": ecs.Secret.from_secrets_manager(self.db_secret, "username"),
                            "MAGENTO_DB_MASTER_PASSWORD": ecs.Secret.from_secrets_manager(self.db_secret, "password"),
                            "MAGENTO_ADMIN_USER": ecs.Secret.from_secrets_manager(self.app_secret, "admin_user"),
                            "MAGENTO_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(self.app_secret, "admin_password"),
                        }
                        if command is not None else {}
                    ),
                },
            )
            container.add_mount_points(
                ecs.MountPoint(source_volume="media", container_path="/var/www/magento/pub/media", read_only=False),
                ecs.MountPoint(source_volume="var", container_path="/var/www/magento/var", read_only=False),
            )
            return td

        self.web_td = task_definition("WebTask", n("web"), env_cfg.task_cpu, env_cfg.task_memory_mib, None)
        # One-off task for setup:install / setup:upgrade and cache:flush (command chosen at run time).
        self.ops_td = task_definition("OpsTask", n("ops"), env_cfg.task_cpu, env_cfg.task_memory_mib,
                                      ["/usr/local/bin/magento-migrate"])
        self._lock_secrets()

        # ------------------------------------------------------------------ ALB (internal only)
        alb_logs = s3.Bucket(
            self, "AlbLogs",
            bucket_name=None,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            removal_policy=removal,
            auto_delete_objects=app_cfg.destroy_on_delete,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30 if app_cfg.destroy_on_delete else 365))],
        )
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=n("alb"), vpc=self.vpc, internet_facing=False,
            vpc_subnets=app_subnets, security_group=self.alb_sg, drop_invalid_header_fields=True,
        )
        self.alb.log_access_logs(alb_logs, prefix="alb")
        if env_cfg.https:
            cert_arn = env_cfg.certificate_arn
            if cert_arn == SYNTH_CHECK_CERTIFICATE:  # template checks only; never a real certificate
                cert_arn = self.format_arn(service="acm", resource="certificate", resource_name=cert_arn)
            self.listener = self.alb.add_listener(
                "Https", port=443, protocol=elbv2.ApplicationProtocol.HTTPS, open=False,
                certificates=[elbv2.ListenerCertificate.from_arn(cert_arn)],
                ssl_policy=elbv2.SslPolicy.TLS13_12_RES_PQ,  # ELB-recommended post-quantum TLS 1.3/1.2 policy
                default_action=elbv2.ListenerAction.fixed_response(404),
            )
            self.alb.add_listener("HttpRedirect", port=80, open=False, default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True))
        else:
            self.listener = self.alb.add_listener("Http", port=80, open=False,
                                                  default_action=elbv2.ListenerAction.fixed_response(404))
        health = elbv2.HealthCheck(path="/health_check.php", healthy_http_codes="200",
                                   interval=Duration.seconds(15), healthy_threshold_count=2)

        def tg(construct_id: str) -> elbv2.ApplicationTargetGroup:
            return elbv2.ApplicationTargetGroup(
                self, construct_id, vpc=self.vpc, port=WEB_PORT, protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP, health_check=health,
                deregistration_delay=Duration.seconds(30),
            )

        self.blue_tg = tg("BlueTg")
        self.green_tg: elbv2.ApplicationTargetGroup | None = None
        prod_rule = elbv2.ApplicationListenerRule(
            self, "ProdRule", listener=self.listener, priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
            action=elbv2.ListenerAction.forward([self.blue_tg]),
        )
        # HTTPS: the certificate's host name (a private DNS record pointing at the internal ALB is
        # the operator's job, see docs/handover.md). HTTP: the ALB's own DNS name.
        site_host = env_cfg.site_hostname if env_cfg.https else self.alb.load_balancer_dns_name
        base_url = f"{env_cfg.base_url_scheme}://{site_host}/"
        ssm_base_url.node.default_child.add_property_override("Value", base_url)

        # ------------------------------------------------------------------ ECS service
        running = image_tag is not None
        self.service = ecs.FargateService(
            self,
            "WebService",
            service_name=n("web"),
            cluster=self.cluster,
            task_definition=self.web_td,
            desired_count=env_cfg.desired_count if running else 0,
            vpc_subnets=app_subnets,
            security_groups=[self.web_sg],
            assign_public_ip=False,
            enable_execute_command=False,
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True)
            if env_cfg.deployment == "rolling" else None,
            health_check_grace_period=Duration.seconds(120),
            platform_version=ecs.FargatePlatformVersion.LATEST,
            **(
                {"deployment_strategy": ecs.DeploymentStrategy.BLUE_GREEN,
                 "bake_time": Duration.minutes(env_cfg.bake_time_minutes)}
                if env_cfg.deployment == "blue_green" else {}
            ),
        )
        if env_cfg.deployment == "blue_green":
            # Amazon ECS native blue/green: ECS registers the new revision in the green target
            # group, shifts the production listener rule, keeps blue for the bake time, and
            # rolls back by shifting the rule back.
            self.green_tg = tg("GreenTg")
            target = self.service.load_balancer_target(
                container_name="web",
                container_port=WEB_PORT,
                alternate_target=ecs.AlternateTarget(
                    "AlternateTarget",
                    alternate_target_group=self.green_tg,
                    production_listener=ecs.ListenerRuleConfiguration.application_listener_rule(prod_rule),
                ),
            )
            target.attach_to_application_target_group(self.blue_tg)
        else:
            self.service.attach_to_application_target_group(self.blue_tg)
        self.service.node.add_dependency(prod_rule)

        # ------------------------------------------------------------------ monitoring
        self._alarms()
        rule = events.Rule(
            self, "EcsDeployFailed",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Deployment State Change"],
                detail={"eventName": ["SERVICE_DEPLOYMENT_FAILED"]},
                resources=[self.service.service_arn],
            ),
        )
        rule.add_target(targets.SnsTopic(self.alarm_topic))

        # Image tag currently running, written by the deploy script after a successful promotion
        # (CloudFormation only creates it; read it before re-running cdk deploy, see deployment guide).
        self.current_tag_param = ssm.StringParameter(
            self, "CurrentImageTag", parameter_name=app_cfg.ssm(env, "current_image_tag"),
            string_value="bootstrap", description="Image tag last promoted by the deploy script",
        )

        # ------------------------------------------------------------------ deploy project (in VPC)
        self.deploy_project = self._deploy_project(n, app_subnets, log_retention, removal)

        CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "ImageTag", value=self.image_tag)
        # Deploy metadata consumed by scripts/deploy/ecs_deploy.sh (no cross-stack exports).
        ssm.StringParameter(
            self, "DeployMeta", parameter_name=app_cfg.ssm(env, "deploy"),
            description="Deployment metadata for the pipeline deploy script",
            string_value=self.to_json_string({
                "cluster": self.cluster.cluster_name,
                "service": self.service.service_name,
                "webFamily": n("web"),
                "opsFamily": n("ops"),
                "subnets": [s.subnet_id for s in self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets],
                "securityGroup": self.web_sg.security_group_id,
                "albDns": self.alb.load_balancer_dns_name,
                "dbInstance": self.db.instance_identifier,
                "snapshotBeforeMigrate": env_cfg.snapshot_before_migrate,
                "desiredCount": env_cfg.desired_count,
                "deployment": env_cfg.deployment,
                "namePrefix": n(),
                "predeploySnapshotsKeep": env_cfg.predeploy_snapshots_keep,
                "maintenanceOnFailure": env_cfg.maintenance_on_failure,
                "scheme": env_cfg.base_url_scheme,
                "siteHost": site_host,
                "snapshotPrefix": n("predeploy"),
            }),
        )

    # ---------------------------------------------------------------------- helpers
    def _lock_secrets(self) -> None:
        """Resource policies: only the task execution roles that inject a secret (and the CDK
        CloudFormation execution role, for secrets CloudFormation resolves as dynamic references)
        may read it; every other principal is explicitly denied, including administrators."""
        stack = Stack.of(self)
        cfn_exec = f"arn:{stack.partition}:iam::{stack.account}:role/cdk-*-cfn-exec-role-*"
        web, ops = self.web_td.obtain_execution_role().role_arn, self.ops_td.obtain_execution_role().role_arn
        readers = {
            self.db_secret: [ops, cfn_exec],  # master user: migration task + RDS dynamic reference
            self.db_app_secret: [web, ops],
            self.app_secret: [ops],
            self.crypt_secret: [web, ops],
            self.search_secret: [web, ops, cfn_exec],  # OpenSearch master password dynamic reference
            self.cache_secret: [web, ops, cfn_exec],  # Valkey AUTH token dynamic reference
        }
        for secret, allowed in readers.items():
            secret.add_to_resource_policy(iam.PolicyStatement(
                sid="DenyReadExceptConsumers", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
                actions=["secretsmanager:GetSecretValue"], resources=["*"],
                conditions={"StringNotLike": {"aws:PrincipalArn": allowed}},
            ))

    def _alarms(self) -> None:
        action = cw_actions.SnsAction(self.alarm_topic)

        def alarm(cid: str, metric: cw.IMetric, threshold: float, desc: str,
                  cmp=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, periods: int = 3):
            a = cw.Alarm(self, cid, metric=metric, threshold=threshold, evaluation_periods=periods,
                         comparison_operator=cmp, alarm_description=desc,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
            a.add_alarm_action(action)
            return a

        t = self.env_cfg.alarms
        alarm("Alb5xx", self.alb.metrics.http_code_target(elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
                                                          period=Duration.minutes(1)), t.alb_target_5xx_per_minute,
              "Magento returned 5xx responses")
        # One alarm per target group: with blue/green the active group alternates between deployments.
        for cid, group in (("UnhealthyTargets", self.blue_tg), ("UnhealthyTargetsGreen", self.green_tg)):
            if group is not None:
                # Explicit dimensions: the green group is attached by ECS, not by a CDK listener.
                metric = cw.Metric(namespace="AWS/ApplicationELB", metric_name="UnHealthyHostCount",
                                   dimensions_map={"TargetGroup": group.target_group_full_name,
                                                   "LoadBalancer": self.alb.load_balancer_full_name},
                                   period=Duration.minutes(1), statistic="Maximum")
                alarm(cid, metric, 1, f"ALB has unhealthy Magento targets ({group.node.id})")
        alarm("EcsCpuHigh", self.service.metric_cpu_utilization(period=Duration.minutes(5)), t.cpu_percent,
              "ECS CPU high")
        alarm("EcsMemHigh", self.service.metric_memory_utilization(period=Duration.minutes(5)), t.memory_percent,
              "ECS memory high")
        alarm("DbCpuHigh", self.db.metric_cpu_utilization(period=Duration.minutes(5)), t.cpu_percent, "RDS CPU high")
        alarm("DbStorageLow", self.db.metric_free_storage_space(period=Duration.minutes(5)),
              t.db_free_storage_gib * 1024**3,
              "RDS free storage low", cmp=cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD)
        alarm("SearchRed", self.search.metric_cluster_status_red(period=Duration.minutes(1)), 1,
              "OpenSearch cluster status red", periods=1)
        alarm("CacheCpuHigh", cw.Metric(namespace="AWS/ElastiCache", metric_name="EngineCPUUtilization",
                                        dimensions_map={"ReplicationGroupId": self.cache.ref},
                                        period=Duration.minutes(5), statistic="Average"), t.cpu_percent,
              "Valkey engine CPU high")

    def _deploy_project(self, n, app_subnets, log_retention, removal) -> codebuild.Project:
        env = self.env_cfg.name
        log_group = logs.LogGroup(self, "DeployLogs", log_group_name=f"/{self.app_cfg.project}/{env}/deploy",
                                  retention=log_retention, removal_policy=removal)
        project = codebuild.PipelineProject(
            self,
            "DeployProject",
            project_name=n("deploy"),
            description=f"Promote an image to {env}: migrate once, update service, flush cache, smoke test",
            vpc=self.vpc,
            subnet_selection=app_subnets,
            security_groups=[self.deploy_sg],
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.from_code_build_image_id(self.app_cfg.build.codebuild_image),
                compute_type=codebuild.ComputeType.SMALL,
            ),
            environment_variables={
                "TARGET_ENV": codebuild.BuildEnvironmentVariable(value=env),
                "SSM_PREFIX": codebuild.BuildEnvironmentVariable(value=self.app_cfg.ssm_prefix),
                # the deploy re-checks the image's scan result with the same gate as the build
                "BLOCK_SEVERITIES": codebuild.BuildEnvironmentVariable(
                    value=",".join(self.app_cfg.build.block_on_severities)),
                "SCAN_UNAVAILABLE_ACTION": codebuild.BuildEnvironmentVariable(
                    value=self.app_cfg.build.scan_unavailable_action),
            },
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {"build": {"commands": [
                    "echo \"[deploy] trigger: ${PROMOTE_REASON:-pipeline}\"",
                    "bash deploy/ecs_deploy.sh \"$TARGET_ENV\" imageDetail.json",
                    "bash deploy/smoke_test.sh \"$TARGET_ENV\"",
                ]}},
            }),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group)),
            timeout=Duration.minutes(90),
        )
        role = project.role
        stack = Stack.of(self)
        arn = lambda svc, res: f"arn:{stack.partition}:{svc}:{stack.region}:{stack.account}:{res}"  # noqa: E731
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[arn("ssm", f"parameter{self.app_cfg.ssm_prefix}/{env}/*")],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"],
            resources=["*"],  # these ECS actions do not support resource-level permissions
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecs:UpdateService", "ecs:DescribeServices"],
            resources=[self.service.service_arn],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"],
            resources=[arn("ecs", f"task-definition/{n('ops')}:*")],
            conditions={"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}},
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecs:DescribeTasks", "ecs:ListTasks", "ecs:StopTask"],  # StopTask: timed-out one-off tasks
            resources=[arn("ecs", f"task/{self.cluster.cluster_name}/*"),
                       arn("ecs", f"container-instance/{self.cluster.cluster_name}/*")],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecs:ListTasks"], resources=["*"],
            conditions={"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}},
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[self.web_td.task_role.role_arn, self.web_td.obtain_execution_role().role_arn,
                       self.ops_td.task_role.role_arn, self.ops_td.obtain_execution_role().role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
        ))
        # Read the pipeline's build artifact (imageDetail.json + deploy scripts).
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[f"arn:{stack.partition}:s3:::{artifact_bucket_name(self.app_cfg, stack)}/*"],
        ))
        # Re-check the promoted image (tag present, scan result) before changing anything.
        repo_arn = arn("ecr", f"repository/{self.app_cfg.name('magento')}")
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ecr:DescribeImages", "ecr:DescribeImageScanFindings"], resources=[repo_arn],
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["inspector2:ListCoverage", "inspector2:ListFindings"],
            resources=["*"],  # enhanced-scanning results; Inspector list actions have no resource-level permissions
        ))
        role.add_to_principal_policy(iam.PolicyStatement(
            actions=["ssm:PutParameter"], resources=[self.current_tag_param.parameter_arn],
        ))
        if self.env_cfg.snapshot_before_migrate:
            role.add_to_principal_policy(iam.PolicyStatement(
                actions=["rds:CreateDBSnapshot", "rds:DescribeDBSnapshots", "rds:AddTagsToResource",
                         "rds:DeleteDBSnapshot"],  # delete: prune to predeploy_snapshots_keep
                resources=[self.db.instance_arn, arn("rds", f"snapshot:{n('predeploy')}-*")],
            ))
        return project
