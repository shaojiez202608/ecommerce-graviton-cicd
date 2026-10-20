"""Build once, promote everywhere: ECR, S3 source bucket, arm64 CodeBuild image build, and one
CodePipeline (V2) per configured branch flow with manual approvals and stage rollback.
Stack: proto-ecom-cicd. Per-environment deploy projects live in each platform stack and are
referenced by name, so this stack has no cross-stack exports.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as cpactions
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .config import AppConfig


class CicdStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, app_cfg: AppConfig, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        cfg = app_cfg
        self.cfg = app_cfg
        destroy = cfg.destroy_on_delete
        removal = RemovalPolicy.DESTROY if destroy else RemovalPolicy.RETAIN
        retention = logs.RetentionDays.ONE_WEEK if destroy else logs.RetentionDays.THREE_MONTHS

        self.repo = ecr.Repository(
            self, "Repo", repository_name=cfg.name("magento"),
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.AES_256,
            removal_policy=removal,
            empty_on_delete=destroy,
            lifecycle_rules=[ecr.LifecycleRule(description="keep recent images", max_image_count=30)],
        )

        bucket_props = dict(
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=removal,
            auto_delete_objects=destroy,
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(30))],
        )
        self.source_bucket = s3.Bucket(self, "SourceBucket", event_bridge_enabled=True, **bucket_props)
        self.artifact_bucket = s3.Bucket(self, "ArtifactBucket", **bucket_props)

        self.approval_topic = sns.Topic(self, "Approvals", topic_name=cfg.name("approvals"), enforce_ssl=True)
        if cfg.build.alarm_email:
            self.approval_topic.add_subscription(subs.EmailSubscription(cfg.build.alarm_email))

        build_logs = logs.LogGroup(self, "BuildLogs", log_group_name=f"/{cfg.project}/build",
                                   retention=retention, removal_policy=removal)
        self.build_project = codebuild.PipelineProject(
            self, "ImageBuild",
            project_name=cfg.name("image-build"),
            description="Build the immutable Magento image on arm64 and push it to ECR (tag = commit SHA)",
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.from_code_build_image_id(cfg.build.codebuild_image),
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,  # required for docker build
            ),
            environment_variables={
                "IMAGE_REPO_URI": codebuild.BuildEnvironmentVariable(value=self.repo.repository_uri),
                "IMAGE_REPO_NAME": codebuild.BuildEnvironmentVariable(value=self.repo.repository_name),
                "IMAGE_VARIANT": codebuild.BuildEnvironmentVariable(value=cfg.build.image_variant),
                "MAGENTO_REF": codebuild.BuildEnvironmentVariable(value=cfg.build.magento_ref),
                "PHP_IMAGE": codebuild.BuildEnvironmentVariable(value=cfg.build.php_image),
                "BLOCK_SEVERITIES": codebuild.BuildEnvironmentVariable(value=",".join(cfg.build.block_on_severities)),
            },
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec/build.yml"),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=build_logs)),
            timeout=Duration.minutes(120),
        )
        self.repo.grant_pull_push(self.build_project)
        self.build_project.add_to_role_policy(iam.PolicyStatement(
            actions=["ecr:DescribeImages", "ecr:DescribeImageScanFindings"],
            resources=[self.repo.repository_arn],
        ))
        # Authenticated pulls from the ECR Public mirror of Docker Official Images.
        self.build_project.add_to_role_policy(iam.PolicyStatement(
            actions=["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"],
            resources=["*"],  # these token actions do not support resource-level permissions
        ))

        self.pipelines: dict[str, codepipeline.Pipeline] = {}
        for p in cfg.pipelines:
            self.pipelines[p.name] = self._pipeline(p)
        CfnOutput(self, "SourceBucketName", value=self.source_bucket.bucket_name)
        CfnOutput(self, "RepositoryUri", value=self.repo.repository_uri)

    def _pipeline(self, p) -> codepipeline.Pipeline:
        app_cfg: AppConfig = self.cfg
        source_out = codepipeline.Artifact("Source")
        build_out = codepipeline.Artifact("Image")
        pipeline = codepipeline.Pipeline(
            self, f"Pipeline-{p.name}",
            pipeline_name=app_cfg.name("pipeline", p.name),
            pipeline_type=codepipeline.PipelineType.V2,
            execution_mode=codepipeline.ExecutionMode.QUEUED,
            artifact_bucket=self.artifact_bucket,
            cross_account_keys=False,
            restart_execution_on_update=False,
        )
        pipeline.add_stage(stage_name="Source", actions=[cpactions.S3SourceAction(
            action_name="Source", bucket=self.source_bucket, bucket_key=p.source_key, output=source_out,
            trigger=cpactions.S3Trigger.NONE,  # started by the EventBridge rule below
        )])
        pipeline.add_stage(stage_name="Build", actions=[cpactions.CodeBuildAction(
            action_name="BuildImageArm64", project=self.build_project, input=source_out, outputs=[build_out],
            variables_namespace="Build",
        )])
        deploy_stage_indexes = []
        for env in p.environments:
            reason = p.approvals_before.get(env)
            if reason:
                pipeline.add_stage(stage_name=f"Approve-{env}", actions=[cpactions.ManualApprovalAction(
                    action_name=f"Approve-{env}",
                    additional_information=f"{reason}: promote image #{{Build.IMAGE_TAG}} to {env}",
                    notification_topic=self.approval_topic,
                )])
            project = codebuild.Project.from_project_name(self, f"Deploy-{p.name}-{env}",
                                                          app_cfg.name(env, "deploy"))
            pipeline.add_stage(stage_name=f"Deploy-{env}", actions=[cpactions.CodeBuildAction(
                action_name=f"Deploy-{env}", project=project, input=build_out,
                environment_variables={
                    "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value="#{Build.IMAGE_TAG}"),
                },
            )])
            deploy_stage_indexes.append(len(pipeline.stages) - 1)

        cfn: codepipeline.CfnPipeline = pipeline.node.default_child
        for idx in deploy_stage_indexes:
            # A failed deploy stage rolls back to the last successful execution of that stage
            # (same image tag/artifacts as before). See docs/architecture.md "Rollback".
            cfn.add_property_override(f"Stages.{idx}.OnFailure", {"Result": "ROLLBACK"})

        events.Rule(
            self, f"SourceUploaded-{p.name}",
            event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [self.source_bucket.bucket_name]}, "object": {"key": [p.source_key]}},
            ),
            targets=[targets.CodePipeline(pipeline)],
        )
        return pipeline
