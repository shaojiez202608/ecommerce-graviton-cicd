"""Storefront <-> Amazon MCF fulfillment bridge: EventBridge bus, SQS (+DLQ), DynamoDB state table,
three arm64 Lambda functions, placeholder SP-API secret, alarms. Stack: proto-ecom-<env>-mcf.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import AppConfig, EnvConfig

SRC = Path(__file__).resolve().parents[2] / "src" / "mcf"
STOREFRONT_SOURCE = "storefront.orders"


class McfStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, app_cfg: AppConfig, env_cfg: EnvConfig, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        env, mcf = env_cfg.name, env_cfg.mcf
        n = lambda *p: app_cfg.name(env, *p)  # noqa: E731
        removal = RemovalPolicy.DESTROY if app_cfg.destroy_on_delete else RemovalPolicy.RETAIN
        retention = logs.RetentionDays.ONE_WEEK if app_cfg.destroy_on_delete else logs.RetentionDays.THREE_MONTHS

        self.table = ddb.TableV2(
            self, "State", table_name=n("mcf-state"),
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            global_secondary_indexes=[ddb.GlobalSecondaryIndexPropsV2(
                index_name="gsi1",
                partition_key=ddb.Attribute(name="gsi1pk", type=ddb.AttributeType.STRING),
                sort_key=ddb.Attribute(name="gsi1sk", type=ddb.AttributeType.STRING),
            )],
            billing=ddb.Billing.on_demand(),
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            encryption=ddb.TableEncryptionV2.aws_managed_key(),
            removal_policy=removal,
            deletion_protection=not app_cfg.destroy_on_delete,
        )

        self.bus = events.EventBus(self, "Bus", event_bus_name=n("commerce"))

        self.topic = sns.Topic(self, "Alerts", topic_name=n("mcf-alerts"), enforce_ssl=True)
        if app_cfg.build.alarm_email:
            self.topic.add_subscription(subs.EmailSubscription(app_cfg.build.alarm_email))

        # Placeholder only: the value types are documented, the values are not real credentials.
        self.secret = sm.Secret(
            self, "SpApiSecret", secret_name=n("spapi-credentials"),
            description="SP-API LWA app credentials (placeholders; replace before live mode)",
            secret_object_value={
                "lwa_client_id": SecretValue.unsafe_plain_text("REPLACE_ME"),
                "lwa_client_secret": SecretValue.unsafe_plain_text("REPLACE_ME"),
                "lwa_refresh_token": SecretValue.unsafe_plain_text("REPLACE_ME"),
            },
            removal_policy=removal,
        )

        dlq = sqs.Queue(
            self, "IntakeDlq", queue_name=n("mcf-intake-dlq"), retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True, removal_policy=removal,
        )
        self.queue = sqs.Queue(
            self, "IntakeQueue", queue_name=n("mcf-intake"),
            visibility_timeout=Duration.seconds(180),
            encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=mcf.max_attempts),
            removal_policy=removal,
        )
        events.Rule(
            self, "OrderPaidRule", event_bus=self.bus,
            event_pattern=events.EventPattern(source=[STOREFRONT_SOURCE], detail_type=["Order Paid"]),
            targets=[targets.SqsQueue(self.queue)],
        )

        # Stand-in for the storefront consumer: keep bridge output events in a log group.
        out_logs = logs.LogGroup(self, "BridgeEvents", log_group_name=f"/aws/events/{n('mcf-bridge-events')}",
                                 retention=retention, removal_policy=removal)
        events.Rule(
            self, "BridgeEventsRule", event_bus=self.bus,
            event_pattern=events.EventPattern(source=["mcf.bridge"]),
            targets=[targets.CloudWatchLogGroup(out_logs)],
        )
        exceptions = events.Rule(
            self, "ExceptionRule", event_bus=self.bus,
            event_pattern=events.EventPattern(source=["mcf.bridge"], detail_type=["Fulfillment Exception"]),
        )
        exceptions.add_target(targets.SnsTopic(self.topic))

        env_vars = {
            "TABLE_NAME": self.table.table_name,
            "EVENT_BUS_NAME": self.bus.event_bus_name,
            "SPAPI_MODE": mcf.spapi_mode,
            "SPAPI_REGION": mcf.spapi_region,
            "SPAPI_SECRET_ARN": self.secret.secret_arn,
            "MARKETPLACE_IDS": json.dumps(mcf.marketplaces),
            "FULFILLMENT_POLICY": mcf.fulfillment_policy,
            "MAX_ATTEMPTS": str(mcf.max_attempts),
            "MOCK_SHIP_AFTER_SECONDS": str(mcf.mock_ship_after_seconds),
        }

        def fn(cid: str, name: str, handler: str, timeout: int) -> lambda_.Function:
            lg = logs.LogGroup(self, f"{cid}Logs", log_group_name=f"/aws/lambda/{n(name)}",
                               retention=retention, removal_policy=removal)
            f = lambda_.Function(
                self, cid, function_name=n(name),
                runtime=lambda_.Runtime.PYTHON_3_14,
                architecture=lambda_.Architecture.ARM_64,
                handler=f"mcf_bridge.handlers.{handler}",
                code=lambda_.Code.from_asset(str(SRC), exclude=["**/__pycache__", "**/*.pyc"]),
                timeout=Duration.seconds(timeout),
                memory_size=256,
                environment=env_vars,
                log_group=lg,
            )
            self.table.grant_read_write_data(f)
            self.bus.grant_put_events_to(f)
            if mcf.spapi_mode == "live":
                self.secret.grant_read(f)
            return f

        self.intake = fn("IntakeFn", "mcf-intake", "order_intake", 30)
        self.intake.add_event_source(sources.SqsEventSource(
            self.queue, batch_size=5, report_batch_item_failures=True, max_concurrency=2,
        ))
        self.tracking = fn("TrackingFn", "mcf-tracking", "tracking_poller", 120)
        self.inventory = fn("InventoryFn", "mcf-inventory", "inventory_sync", 300)
        events.Rule(self, "TrackingSchedule",
                    schedule=events.Schedule.rate(Duration.minutes(mcf.tracking_poll_minutes)),
                    targets=[targets.LambdaFunction(self.tracking)])
        events.Rule(self, "InventorySchedule",
                    schedule=events.Schedule.rate(Duration.minutes(mcf.inventory_sync_minutes)),
                    targets=[targets.LambdaFunction(self.inventory)])

        CfnOutput(self, "EventBusName", value=self.bus.event_bus_name)
        CfnOutput(self, "StateTableName", value=self.table.table_name)
        CfnOutput(self, "BridgeEventsLogGroup", value=out_logs.log_group_name)
        action = cw_actions.SnsAction(self.topic)
        for cid, metric, desc in (
            ("DlqNotEmpty", dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
             "Order events gave up after retries and are in the DLQ"),
            ("OrderExceptions", cw.Metric(namespace="EcomMcfBridge", metric_name="McfOrderException",
                                          period=Duration.minutes(5), statistic="Sum"),
             "Orders marked EXCEPTION (manual handling required)"),
            ("InvalidSkuMappings", cw.Metric(namespace="EcomMcfBridge", metric_name="McfInvalidSkuMapping",
                                             period=Duration.hours(1), statistic="Sum"),
             "Storefront SKUs mapped to MSKUs that FBA inventory does not know"),
            ("IntakeErrors", self.intake.metric_errors(period=Duration.minutes(5)), "Intake Lambda errors"),
            ("TrackingErrors", self.tracking.metric_errors(period=Duration.minutes(15)), "Tracking Lambda errors"),
        ):
            alarm = cw.Alarm(self, cid, metric=metric, threshold=1, evaluation_periods=1,
                             comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                             treat_missing_data=cw.TreatMissingData.NOT_BREACHING, alarm_description=desc)
            alarm.add_alarm_action(action)
