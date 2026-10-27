from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import pubsub_v1
from google.oauth2 import service_account
import google.auth
import google.auth.exceptions
import google.api_core.exceptions
import os
import ast
import sys
import json
from AppConfig import AppConfig
from services import serviceGetter

# ---------- Data model ----------


@dataclass
class Subscription:
    """
    Describes a push subscription to create.
    - name: subscription ID (not the full path). Must be unique within the project.
    - endpoint: HTTPS URL for Pub/Sub push delivery.
    - audience: (Optional) OIDC 'aud' to validate on the receiver (often your endpoint URL or a fixed string).
    - dead_letter_topic: (Optional) topic ID or full topic path to receive DLQs (must exist).
    - max_delivery_attempts: (Optional) DLQ attempts before dead-lettering (1–100).
    - ack_deadline_seconds: 10–600 (default 10). Push still needs an ack from your service.
    - message_retention_seconds: 600–604800 (default 604800 = 7 days).
    - min_backoff_seconds / max_backoff_seconds: retry policy (0–600).
    """
    name: str
    endpoint: str
    audience: Optional[str] = None

    dead_letter_topic: Optional[str] = None
    max_delivery_attempts: int = 5

    ack_deadline_seconds: int = 10
    message_retention_seconds: int = 7 * 24 * 60 * 60  # 7 days

    # Retry policy defaults: enable exponential backoff by default
    min_backoff_seconds: Optional[int] = 10
    max_backoff_seconds: Optional[int] = 600

    labels: Dict[str, str] = field(default_factory=dict)


# ---------- Admin helpers ----------


def __ensure_topic(publisher: pubsub_v1.PublisherClient, project_id: str,
                   topic_name: str) -> str:
    if topic_name.startswith("projects/"):
        topic_path = topic_name
    else:
        topic_path = publisher.topic_path(project_id, topic_name)

    try:
        publisher.get_topic(request={"topic": topic_path})
    except NotFound:
        try:
            publisher.create_topic(request={"name": topic_path})
        except AlreadyExists:
            pass
    return topic_path


def __ensure_subscription(subscriber: pubsub_v1.SubscriberClient,
                          project_id: str, sub: Subscription, topic_path: str,
                          service_account_email: str,
                          publisher: pubsub_v1.PublisherClient,
                          allowPatch: bool = True) -> Optional[str]:
    if sub.name.startswith("projects/"):
        subscription_path = sub.name
    else:
        subscription_path = subscriber.subscription_path(project_id, sub.name)

    push_cfg = pubsub_v1.types.PushConfig(
        push_endpoint=sub.endpoint,
        oidc_token=pubsub_v1.types.PushConfig.OidcToken(
            service_account_email=service_account_email,
            audience=sub.endpoint,
        ),
    )

    dl_policy = None
    if sub.dead_letter_topic:
        if sub.dead_letter_topic.startswith("projects/"):
            dl_topic_path = sub.dead_letter_topic
        else:
            dl_topic_path = subscriber.topic_path(project_id,
                                                  sub.dead_letter_topic)
        try:
            publisher.get_topic(request={"topic": dl_topic_path})
        except NotFound:
            raise ValueError(
                f"Dead-letter topic not found: {dl_topic_path}. Create the topic first or remove dead_letter_topic."
            )
        dl_policy = pubsub_v1.types.DeadLetterPolicy(
            dead_letter_topic=dl_topic_path,
            max_delivery_attempts=sub.max_delivery_attempts or 5,
        )

    eff_min = sub.min_backoff_seconds if sub.min_backoff_seconds is not None else 10
    eff_max = sub.max_backoff_seconds if sub.max_backoff_seconds is not None else 600
    if eff_min < 0:
        eff_min = 0
    if eff_max > 600:
        eff_max = 600
    if eff_max < eff_min:
        eff_max = eff_min
    retry_policy = pubsub_v1.types.RetryPolicy(
        minimum_backoff={"seconds": eff_min},
        maximum_backoff={"seconds": eff_max},
    )

    try:
        subscriber.get_subscription(
            request={"subscription": subscription_path})
        exists = True
    except NotFound:
        exists = False

    # New-only never touches an existing subscription, e.g. one created
    # between the review table and this apply
    if exists and not allowPatch:
        print(f"{subscription_path} exists -> skipped (new-only)")
        return None

    if not exists:
        create_req = {
            "name": subscription_path,
            "topic": topic_path,
            "push_config": push_cfg,
            "ack_deadline_seconds": sub.ack_deadline_seconds,
            "labels": sub.labels,
            "retry_policy": retry_policy,
            "dead_letter_policy": dl_policy,
            "message_retention_duration": {
                "seconds": sub.message_retention_seconds
            },
        }
        create_req = {
            k: v
            for k, v in create_req.items() if v not in (None, "", {})
        }
        # Never expire: an ExpirationPolicy with no ttl disables the default
        # 31-day inactivity deletion (added after the empty-value filter so
        # it cannot be stripped)
        create_req["expiration_policy"] = pubsub_v1.types.ExpirationPolicy()
        try:
            subscriber.create_subscription(request=create_req)
        except AlreadyExists:
            pass
    else:
        update_mask_paths = [
            "push_config",
            "ack_deadline_seconds",
            "retry_policy",
            "dead_letter_policy",
            "message_retention_duration",
            "labels",
            "expiration_policy",
        ]
        sub_obj = pubsub_v1.types.Subscription(
            name=subscription_path,
            push_config=push_cfg,
            ack_deadline_seconds=sub.ack_deadline_seconds,
            retry_policy=retry_policy,
            dead_letter_policy=dl_policy,
            message_retention_duration={
                "seconds": sub.message_retention_seconds
            },
            labels=sub.labels,
            expiration_policy=pubsub_v1.types.ExpirationPolicy(),
        )
        subscriber.update_subscription(request={
            "subscription": sub_obj,
            "update_mask": {
                "paths": update_mask_paths
            },
        })

    return subscription_path


def __ensure_binding(bindings, role, member):
    for b in bindings:
        if getattr(b, "role", None) == role:
            if member not in b.members:
                b.members.append(member)
            return
    if hasattr(bindings, "add"):
        new_b = bindings.add()
        new_b.role = role
        new_b.members.append(member)
    else:
        raise TypeError(
            "Unexpected bindings container type; cannot add IAM binding")


def __grant_dlq_permissions(sub_name: str, dead_letter_topic_name: str,
                            source_project_id: str, credentials):
    """The Pub/Sub service agent forwards a failed push: it must publish to
    the DEAD-LETTER topic and subscribe to the source subscription."""
    from google.cloud import resourcemanager_v3

    crm = resourcemanager_v3.ProjectsClient(credentials=credentials)
    proj = crm.get_project(name=f"projects/{source_project_id}")
    sa = f"service-{proj.name.split('/')[-1]}@gcp-sa-pubsub.iam.gserviceaccount.com"

    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    publisher = pubsub_v1.PublisherClient(credentials=credentials)

    t_policy = publisher.get_iam_policy(request={"resource": dead_letter_topic_name})
    __ensure_binding(t_policy.bindings, "roles/pubsub.publisher",
                     f"serviceAccount:{sa}")
    publisher.set_iam_policy(request={
        "resource": dead_letter_topic_name,
        "policy": t_policy
    })

    s_policy = subscriber.get_iam_policy(request={"resource": sub_name})
    __ensure_binding(s_policy.bindings, "roles/pubsub.subscriber",
                     f"serviceAccount:{sa}")
    subscriber.set_iam_policy(request={
        "resource": sub_name,
        "policy": s_policy
    })


# ---------- Cloud Run URL fetching ----------


def __fetchCloudRunServices(credentials, project_id, region=None):
    if region is None:
        region = AppConfig().gcpRegion
    """Fetch all Cloud Run services from GCP. Returns {name: service_object}."""
    from google.cloud import run_v2
    client = run_v2.ServicesClient(credentials=credentials)
    parent = f"projects/{project_id}/locations/{region}"
    services = list(client.list_services(parent=parent))
    return {svc.name.split('/')[-1]: svc for svc in services}


def __selectCurrentService(cloud_run_services, cachedService=''):
    """Interactive: select which Cloud Run service is the current project."""
    names = list(cloud_run_services.keys())
    name = __pickFromList("Available Cloud Run services:", names, cachedService,
                          "Select the Cloud Run service for THIS project")
    return cloud_run_services[name]


def __getEnvVarFromCloudRun(cloud_run_service, name):
    """Read one env var from a Cloud Run service's container config (None if unset)."""
    for env_var in cloud_run_service.template.containers[0].env:
        if env_var.name == name:
            return env_var.value
    return None


def __getEnabledServicesFromCloudRun(cloud_run_service):
    """Read PUBSUB_ENABLED_* env vars from a Cloud Run service's container config."""
    enabled = {}
    for env_var in cloud_run_service.template.containers[0].env:
        if env_var.name.startswith('PUBSUB_ENABLED_') and env_var.value.lower() == 'true':
            enabled[env_var.name] = True
    return enabled


def __mapServicesToCloudRun(cloud_run_services, enabled_envs):
    """Auto-match enabled local services to Cloud Run services via cloudRunServiceName."""
    import re
    url_map = {}
    for local_svc in serviceGetter.listOfServices:
        upper_snake = re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', local_svc.name).upper()
        env_var_name = 'PUBSUB_ENABLED_' + upper_snake
        if env_var_name not in enabled_envs:
            print(f"  {local_svc.name} -> skipped (PubSub disabled)")
            continue
        cr_name = local_svc.cloudRunServiceName
        if cr_name not in cloud_run_services:
            raise ValueError(
                f"Cloud Run service '{cr_name}' not found for '{local_svc.name}'. "
                f"Available: {list(cloud_run_services.keys())}"
            )
        svc = cloud_run_services[cr_name]
        url = svc.uri.rstrip('/') + '/'
        url_map[local_svc.name] = url
        print(f"  {local_svc.name} -> {cr_name} -> {url}")

    return url_map


# ---------- Public API ----------


def __createPubSub(topicName: str, subscriptions: List[Subscription],
                   credentials, project_id: str,
                   service_account_email: str,
                   allowPatch: bool = True) -> dict:
    publisher = pubsub_v1.PublisherClient(credentials=credentials)
    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    projectId = project_id
    service_account_email = service_account_email

    topic_path = __ensure_topic(publisher, projectId, topicName)

    sub_paths = []
    for s in subscriptions:
        sub_path = __ensure_subscription(subscriber, projectId, s, topic_path,
                                         service_account_email, publisher,
                                         allowPatch)
        if sub_path is None:
            continue
        if s.dead_letter_topic:
            dead_letter_path = (s.dead_letter_topic if s.dead_letter_topic.startswith("projects/")
                                else publisher.topic_path(projectId, s.dead_letter_topic))
            __grant_dlq_permissions(sub_path, dead_letter_path, projectId, credentials)
        sub_paths.append(sub_path)

    print('_________________________________')
    print('Added topic : ', topic_path)
    for x in sub_paths:
        print('Added subscriptions : ', x)
    print('_________________________________')
    print('_________________________________')
    print('_________________________________')

    return {"topic": topic_path, "subscriptions": sub_paths}


def __getAllFunctionsThatHasPubSubDecorator() -> List[Tuple[str, str]]:
    """
        Returns a list of (qualified_name, function_name) tuples for all functions
        decorated with @pubSubFunction, including class methods.
        """

    def getFunctionsFromFile(file_path: str) -> List[ast.FunctionDef]:
        with open(file_path, "r", encoding="utf-8") as file:
            node = ast.parse(file.read(), filename=file_path)

        functions = []

        def visit_body(body, class_prefix="", file_path=''):
            for item in body:
                if isinstance(item, ast.FunctionDef):
                    for decorator in item.decorator_list:

                        if (isinstance(decorator, ast.Name)
                                and decorator.id == "pubSubDecorator"
                            ) or (isinstance(decorator, ast.Call)
                                  and isinstance(decorator.func, ast.Name)
                                  and decorator.func.id == "pubSubDecorator"):

                            decorator_args = {}
                            if isinstance(decorator, ast.Call):
                                for i, arg in enumerate(decorator.args):
                                    arg_value = None
                                    if isinstance(arg, ast.Constant):
                                        arg_value = arg.value
                                    elif isinstance(arg, ast.Name):
                                        arg_value = arg.id
                                    elif isinstance(arg, ast.Str):
                                        arg_value = arg.s
                                    elif isinstance(arg, ast.Num):
                                        arg_value = arg.n
                                    elif isinstance(arg, ast.List):
                                        list_items = []
                                        for elt in arg.elts:
                                            if isinstance(elt, ast.Constant):
                                                list_items.append(elt.value)
                                            elif isinstance(elt, ast.Str):
                                                list_items.append(elt.s)
                                            elif isinstance(elt, ast.Name):
                                                list_items.append(elt.id)
                                        arg_value = list_items
                                    else:
                                        try:
                                            arg_value = ast.unparse(arg)
                                        except AttributeError:
                                            arg_value = str(arg)
                                    decorator_args[f"arg_{i}"] = arg_value

                                for kw in decorator.keywords:
                                    kw_value = None
                                    if isinstance(kw.value, ast.Constant):
                                        kw_value = kw.value.value
                                    elif isinstance(kw.value, ast.Name):
                                        kw_value = kw.value.id
                                    elif isinstance(kw.value, ast.Str):
                                        kw_value = kw.value.s
                                    elif isinstance(kw.value, ast.Num):
                                        kw_value = kw.value.n
                                    elif isinstance(kw.value, ast.List):
                                        list_items = []
                                        for elt in kw.value.elts:
                                            if isinstance(elt, ast.Constant):
                                                list_items.append(elt.value)
                                            elif isinstance(elt, ast.Str):
                                                list_items.append(elt.s)
                                            elif isinstance(elt, ast.Name):
                                                list_items.append(elt.id)
                                        kw_value = list_items
                                    else:
                                        try:
                                            kw_value = ast.unparse(kw.value)
                                        except AttributeError:
                                            kw_value = str(kw.value)
                                    decorator_args[kw.arg] = kw_value

                            print(f"Function: {item.name}")
                            print(f"Decorator arguments: {decorator_args}")

                            if decorator_args.get("publishToProdPubSub",
                                                  None) is False:
                                break

                            foundArgPublishToPubSubMockDb = False
                            for x in item.args.args:
                                if x.arg == "publishToPubSubMockDb":
                                    foundArgPublishToPubSubMockDb = True
                                    break

                            if not foundArgPublishToPubSubMockDb:
                                raise AssertionError(
                                    f"Function {item.name} does not have the argument publishToPubSubMockDb and is a pubSubDecorator."
                                )

                            for arg, default in zip(
                                    item.args.args[-len(item.args.defaults):],
                                    item.args.defaults):
                                if arg.arg == 'publishToProdPubSub':
                                    raise Exception(
                                        f"in file {file_path}, function {item.name} publishToProdPubSub should have no default value so that we can properly set it."
                                    )

                            foundArgPublishProductionPubSub = False
                            for x in item.args.args:
                                if x.arg == "publishToProdPubSub":
                                    foundArgPublishProductionPubSub = True
                                    break

                            if not foundArgPublishProductionPubSub:
                                raise AssertionError(
                                    f"Function {item.name} does not have the argument publishToProdPubSub and is a pubSubDecorator."
                                )

                            functions.append(item)
                elif isinstance(item, ast.ClassDef):
                    visit_body(item.body,
                               class_prefix=item.name,
                               file_path=file_path)

        visit_body(node.body, file_path=file_path)
        return functions

    base_path = os.path.dirname(__file__)
    allFunctions = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            if f.endswith(".py"):
                full_path = os.path.join(root, f)
                functions = getFunctionsFromFile(full_path)
                allFunctions.extend(functions)

    return allFunctions


def __getProjectConsumers(node):
    for deco in node.decorator_list:
        if isinstance(deco, ast.Call):
            name = getattr(deco.func, "id", None) \
                    or getattr(getattr(deco.func, "attr", None), "id", None) \
                    or getattr(deco.func, "attr", None)
            if name == "pubSubDecorator":
                if deco.args:
                    proj_consumers_expr = deco.args[0]
                for kw in deco.keywords:
                    if kw.arg == "projectNameConsumers":
                        return [
                            item.strip() for item in ast.unparse(
                                kw.value).strip("[]").split(",")
                        ]


#MAIN FUNCTION
def createPubSubForAllServices(credentials, project_id: str,
                               service_account_email: str, plan,
                               allowPatch: bool = True):
    """Apply exactly the plan reviewed in __main__: __buildPubSubPlan with
    allowPatch=True, or __newOnlyPlan with allowPatch=False."""
    for topicName, subscriptions in plan:
        __createPubSub(topicName, [sub for _, sub in subscriptions],
                       credentials, project_id, service_account_email,
                       allowPatch)


def __buildPubSubPlan(
        allPubSubFunctions, cloudRunUrlMap,
        deadLetterTopic,
        skipUnmapped: bool = False) -> List[Tuple[str, List[Tuple[str, Subscription]]]]:
    """Pure: [(topicName, [(consumerServiceName, Subscription), ...]), ...] for every
    @pubSubDecorator function.

    Reviewed in __main__ before anything is written, then handed to __createPubSub.
    ``skipUnmapped`` (the non-interactive mode) leaves out a consumer this
    service has not enabled (no PUBSUB_ENABLED_* true, so no URL mapped)
    instead of raising: its own cloud-setup subscribes it."""
    plan = []
    for x in allPubSubFunctions:
        topicName = x.name + '_' + AppConfig().projectName
        servicesToSendTo = __getProjectConsumers(x)
        subscriptionList = []
        for serviceName in servicesToSendTo:
            service = serviceGetter.getServiceByName(serviceName)
            prodUrl = cloudRunUrlMap.get(service.name)
            if prodUrl is None and skipUnmapped:
                print(f"  {x.name} -> {service.name}: skipped, its own cloud-setup subscribes it")
                continue
            if prodUrl is None:
                raise ValueError(
                    f"No Cloud Run URL mapped for service '{service.name}'. "
                    f"Run the Cloud Run mapping step first.")
            name = x.name + '_' + AppConfig(
            ).projectName + '_to_' + service.name
            endpoint = prodUrl + x.name + '_' + AppConfig().projectName
            subscription = Subscription(name=name,
                                        endpoint=endpoint,
                                        audience=endpoint,
                                        dead_letter_topic=deadLetterTopic)
            subscriptionList.append((service.name, subscription))
        plan.append((topicName, subscriptionList))
    return plan


def __pushServiceAccount():
    """The service account every push subscription's OIDC token is minted
    for: callerAuth.PUSH_SERVICE_ACCOUNTS[0], the one the push handlers
    accept, so the two can never disagree. Exits while it is unset."""
    from callerAuth import PUSH_SERVICE_ACCOUNTS
    if not PUSH_SERVICE_ACCOUNTS:
        sys.exit("callerAuth.PUSH_SERVICE_ACCOUNTS is empty: set the push "
                 "service account there first (the push handlers accept no "
                 "other).")
    return PUSH_SERVICE_ACCOUNTS[0]


# ---------- Deploy guardrails: last-used cache, pickers, plan preview ----------
#
# The cache file is shared with server/tools/create_or_update_job.ps1 and
# update_all_jobs.ps1 (see _gcloudTarget.ps1): picking a project/service once
# in any of the three tools makes it the Enter-default in the others. It is
# written only after the user confirms the REVIEW BEFORE DEPLOY summary, and
# never in --dry-run.


def __cachePath():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools',
                        '.last_gcloud_target.json')


def __loadLastTarget():
    """Returns {'project', 'service', 'serviceAccount'} with '' for unknown keys.

    A missing or unreadable file simply means "no cache"."""
    target = {'project': '', 'service': '', 'serviceAccount': ''}
    try:
        # utf-8-sig: PowerShell's Set-Content -Encoding utf8 writes a BOM
        with open(__cachePath(), 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return target
    if isinstance(data, dict):
        for key in target:
            if isinstance(data.get(key), str) and data[key]:
                target[key] = data[key]
    return target


def __saveLastTarget(project, service, serviceAccount):
    """Merge-write so keys written by the PowerShell scripts survive."""
    target = __loadLastTarget()
    target.update({
        'project': project,
        'service': service,
        'serviceAccount': serviceAccount
    })
    path = __cachePath()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(target, f, indent=2)
        f.write('\n')


def __pickFromList(title, options, cachedValue, prompt):
    """Numbered picker shared by the project / service / service-account prompts.

    The cached entry (if still listed) is marked and Enter selects it; any number
    overrides it. Without a cached entry a number is required. Anything else
    exits non-zero so a typo can never fall through to a write."""
    cachedIdx = options.index(cachedValue) if cachedValue in options else -1
    print(f"\n{title}")
    for i, option in enumerate(options):
        if i == cachedIdx:
            print(f"* {i + 1}. {option}  (last used)")
        else:
            print(f"  {i + 1}. {option}")
    if cachedIdx >= 0:
        choice = input(f"\n{prompt} (Enter = {cachedValue}): ").strip()
        if choice == '':
            return options[cachedIdx]
    else:
        choice = input(f"\n{prompt}: ").strip()
        if choice == '':
            sys.exit("No selection made (no last-used entry to default to).")
    try:
        idx = int(choice) - 1
    except ValueError:
        sys.exit(f"Invalid selection: '{choice}'")
    if not 0 <= idx < len(options):
        sys.exit(f"Invalid selection: '{choice}'")
    return options[idx]


def __selectProject(credentials, adc_project_id):
    """Interactive: pick the GCP project. The ADC project is info only, never used implicitly."""
    from google.cloud import resourcemanager_v3

    cached = __loadLastTarget()['project']
    print(f"\nADC default project (info only, not used): {adc_project_id or '<none>'}")
    try:
        crm = resourcemanager_v3.ProjectsClient(credentials=credentials)
        projectIds = sorted(
            p.project_id for p in crm.search_projects()
            if p.state == resourcemanager_v3.Project.State.ACTIVE)
    except google.api_core.exceptions.Unauthenticated:
        raise
    except google.api_core.exceptions.GoogleAPICallError as e:
        print(f"Could not list projects ({e.__class__.__name__}); enter the project ID manually.")
        projectIds = []
    if projectIds:
        return __pickFromList("Available projects:", projectIds, cached,
                              "Select project number")
    suffix = f" (Enter = {cached})" if cached else ''
    projectId = input(f"Enter GCP project ID{suffix}: ").strip()
    if projectId == '' and cached:
        return cached
    if projectId == '':
        sys.exit("No project ID entered.")
    return projectId


def __topicExists(publisher, project_id, topicName):
    try:
        publisher.get_topic(
            request={"topic": publisher.topic_path(project_id, topicName)})
        return True
    except NotFound:
        return False


def __subscriptionExists(subscriber, project_id, subName):
    try:
        subscriber.get_subscription(request={
            "subscription": subscriber.subscription_path(project_id, subName)
        })
        return True
    except NotFound:
        return False


def __fetchExistingState(plan, publisher, subscriber, project_id):
    """Read-only: (existingTopics, existingSubs), the planned topic and
    subscription names that already exist in the project.

    Fetched once in __main__ so the review table and the new-only filter use
    the same snapshot."""
    existingTopics = set()
    existingSubs = set()
    for topicName, subscriptions in plan:
        if __topicExists(publisher, project_id, topicName):
            existingTopics.add(topicName)
        for _, sub in subscriptions:
            if __subscriptionExists(subscriber, project_id, sub.name):
                existingSubs.add(sub.name)
    return existingTopics, existingSubs


def __newOnlyPlan(
        plan, existingTopics,
        existingSubs) -> List[Tuple[str, List[Tuple[str, Subscription]]]]:
    """Pure: the plan minus everything that already exists, in the same shape
    as __buildPubSubPlan.

    Only NEW subscriptions are kept. A topic stays if it is NEW (even with no
    subscriptions left) or still has a NEW subscription, e.g. a consumer added
    to an existing topic's projectNameConsumers."""
    newPlan = []
    for topicName, subscriptions in plan:
        newSubscriptions = [(consumer, sub) for consumer, sub in subscriptions
                            if sub.name not in existingSubs]
        if topicName not in existingTopics or newSubscriptions:
            newPlan.append((topicName, newSubscriptions))
    return newPlan


def __printPlan(plan, existingTopics, existingSubs, publisher, project_id,
                deadLetterTopic, cloudRunUrlMap):
    """Print a compact table of every topic/subscription the run would create or patch.

    Topic/subscription state comes from __fetchExistingState; only the
    dead-letter topic is checked here."""
    dlqState = 'exists' if __topicExists(publisher, project_id, deadLetterTopic) else 'MISSING - create it first or subscriptions will fail'
    print(f"Dead-letter topic: {deadLetterTopic}  [{dlqState}]")

    suffix = '_' + AppConfig().projectName
    print(f"\nNaming:  topic = <function>{suffix}")
    print("         subscription = <topic>_to_<consumer>,  push endpoint = <consumer URL>/<topic>")

    # Rows: (function, topic state, consumer, subscription state); only the
    # first row of a function carries the function/topic cells.
    consumers = []
    rows = []
    topicStates = []
    subStates = []
    for topicName, subscriptions in plan:
        function = topicName[:-len(suffix)] if topicName.endswith(suffix) else topicName
        topicState = 'exists' if topicName in existingTopics else 'NEW'
        topicStates.append(topicState)
        if not subscriptions:
            rows.append((function, topicState, '', ''))
        for i, (consumer, sub) in enumerate(subscriptions):
            if consumer not in consumers:
                consumers.append(consumer)
            subState = 'exists -> PATCH' if sub.name in existingSubs else 'NEW'
            subStates.append(subState)
            rows.append((function if i == 0 else '', topicState if i == 0 else '',
                         consumer, subState))

    print("\nConsumers:")
    consumerWidth = max([len(c) for c in consumers] + [0])
    for consumer in consumers:
        print(f"  {consumer.ljust(consumerWidth)}   {cloudRunUrlMap.get(consumer, '')}")

    header = ('FUNCTION', 'TOPIC', 'CONSUMER', 'SUBSCRIPTION')
    widths = [max(len(row[i]) for row in rows + [header]) for i in range(4)]
    print()
    for row in [header] + rows:
        print('   '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    print(f"Totals: {len(topicStates)} topics ({topicStates.count('exists')} exist, "
          f"{topicStates.count('NEW')} NEW) | {len(subStates)} subscriptions "
          f"({subStates.count('exists -> PATCH')} PATCH, {subStates.count('NEW')} NEW)")


# ---------- Command line ----------
#
# No flags: the interactive pickers and the y / new / abort prompt above.
# --project P --service S [--yes | --dry-run]: no prompt at all (create-star-app
# cloud-setup runs it this way): the project and the Cloud Run service come
# from the flags, --yes applies the full plan (create NEW + PATCH existing),
# --dry-run only prints it, and a missing deadLetterTopic is created instead
# of failing every subscription. The last-used cache is left alone.


def __parseArgs(argv):
    """{'dryRun', 'project', 'service', 'yes'} from argv; flags it does not
    know are ignored, as before the flags existed. Both --flag VALUE and
    --flag=VALUE work."""
    args = {'dryRun': False, 'project': None, 'service': None, 'yes': False}
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == '--dry-run':
            args['dryRun'] = True
        elif arg == '--yes':
            args['yes'] = True
        elif arg.split('=', 1)[0] in ('--project', '--service'):
            key = arg.split('=', 1)[0][2:]
            if '=' in arg:
                value = arg.split('=', 1)[1]
            elif rest:
                value = rest.pop(0)
            else:
                sys.exit(f"--{key} needs a value")
            if not value or value.startswith('-'):
                sys.exit(f"--{key} needs a value")
            args[key] = value
    if (args['service'] or args['yes']) and not args['project']:
        sys.exit("--service and --yes need --project (the non-interactive mode takes the project "
                 "and the Cloud Run service from the flags)")
    if args['project'] and not args['service']:
        sys.exit("--project needs --service (the Cloud Run service of THIS project)")
    return args


def main(argv=None):
    args = __parseArgs(sys.argv[1:] if argv is None else argv)
    dryRun = args['dryRun']
    nonInteractive = args['project'] is not None
    try:
        credentials, adc_project_id = google.auth.default()
        if nonInteractive:
            project_id = args['project']
            lastTarget = {'project': '', 'service': '', 'serviceAccount': ''}
        else:
            project_id = __selectProject(credentials, adc_project_id)
            lastTarget = __loadLastTarget()
        # The cached service / service account only apply to the project they were saved for
        if lastTarget['project'] != project_id:
            lastTarget['service'] = ''
            lastTarget['serviceAccount'] = ''

        cloud_run_services = __fetchCloudRunServices(credentials, project_id)
        if not cloud_run_services:
            sys.exit(f"No Cloud Run services found in project {project_id} - wrong project?")
        if nonInteractive:
            if args['service'] not in cloud_run_services:
                sys.exit(f"Cloud Run service '{args['service']}' not found in project {project_id}. "
                         f"Available: {sorted(cloud_run_services)}")
            current_service = cloud_run_services[args['service']]
        else:
            current_service = __selectCurrentService(cloud_run_services,
                                                     lastTarget['service'])
        current_service_name = current_service.name.split('/')[-1]
        print(f"\nSelected: {current_service_name}")

        enabled_envs = __getEnabledServicesFromCloudRun(current_service)
        print(f"\nEnabled PubSub env vars: {list(enabled_envs.keys())}")

        service_account_email = __pushServiceAccount()
        if nonInteractive and not service_account_email.endswith(
                f"@{project_id}.iam.gserviceaccount.com"):
            sys.exit(f"callerAuth.PUSH_SERVICE_ACCOUNTS[0] is {service_account_email}, not an "
                     f"account of project {project_id}: set this service's own push account there "
                     "first (create-star-app cloud-setup --step local.edits).")
        cloudRunUrlMap = __mapServicesToCloudRun(cloud_run_services,
                                                 enabled_envs)
        allPubSubFunctions = __getAllFunctionsThatHasPubSubDecorator()
        deadLetterTopic = 'deadLetterTopic'
        plan = __buildPubSubPlan(allPubSubFunctions, cloudRunUrlMap,
                                 deadLetterTopic, skipUnmapped=nonInteractive)
        publisher = pubsub_v1.PublisherClient(credentials=credentials)
        existingTopics, existingSubs = __fetchExistingState(
            plan, publisher, pubsub_v1.SubscriberClient(credentials=credentials),
            project_id)

        print("\n==================== REVIEW BEFORE DEPLOY ====================")
        print(f"Project:           {project_id}")
        print(f"Service:           {current_service_name}")
        print(f"Service account:   {service_account_email}")
        serviceProjectId = __getEnvVarFromCloudRun(current_service,
                                                   'GOOGLE_CLOUD_PROJECT_ID')
        if serviceProjectId and serviceProjectId != project_id:
            if nonInteractive:
                sys.exit(f"Service env GOOGLE_CLOUD_PROJECT_ID={serviceProjectId} does not match "
                         f"--project {project_id}: nothing was changed.")
            print(f"WARNING: service env GOOGLE_CLOUD_PROJECT_ID={serviceProjectId} "
                  f"does not match selected project {project_id}")
        __printPlan(plan, existingTopics, existingSubs, publisher, project_id,
                    deadLetterTopic, cloudRunUrlMap)
        print("==============================================================")

        if dryRun:
            print("\nDry run: nothing was changed (and nothing was cached).")
            sys.exit(0)
        if nonInteractive:
            if not args['yes']:
                sys.exit("Non-interactive mode writes only with --yes (or pass --dry-run).")
            __ensure_topic(publisher, project_id, deadLetterTopic)
            createPubSubForAllServices(credentials, project_id,
                                       service_account_email, plan, True)
            return
        # y re-applies the full plan (PATCHes existing subscriptions); new only
        # creates what is missing. new is a whole word so a habitual n aborts.
        newPlan = __newOnlyPlan(plan, existingTopics, existingSubs)
        patchCount = sum(1 for _, subscriptions in plan
                         for _, sub in subscriptions if sub.name in existingSubs)
        newTopicCount = sum(1 for topicName, _ in newPlan
                            if topicName not in existingTopics)
        newSubCount = sum(len(subscriptions) for _, subscriptions in newPlan)
        choice = input(
            f"\nType y   to create NEW + PATCH existing ({patchCount} PATCH)\n"
            f"Type new to create NEW only: {newTopicCount} topics, "
            f"{newSubCount} subscriptions (existing ones untouched)\n"
            "Anything else aborts: ").strip()
        if choice in ('y', 'Y'):
            applyPlan, allowPatch = plan, True
        elif choice.lower() == 'new':
            if not newPlan:
                print("Nothing new to create.")
                sys.exit(0)
            applyPlan, allowPatch = newPlan, False
        else:
            print("Aborted. Nothing was changed.")
            sys.exit(0)
        __saveLastTarget(project_id, current_service_name, service_account_email)
        createPubSubForAllServices(credentials, project_id,
                                   service_account_email, applyPlan, allowPatch)
    except (google.api_core.exceptions.Unauthenticated,
            google.api_core.exceptions.ServiceUnavailable,
            google.api_core.exceptions.RetryError,
            google.auth.exceptions.RefreshError,
            google.auth.exceptions.DefaultCredentialsError) as e:
        print("\nAuthentication failed. Install the Google Cloud SDK if it is missing, then run:")
        print("\n  gcloud auth application-default login\n")
        print(f"({e.__class__.__name__}; nothing more was changed)")
        sys.exit(1)


if __name__ == "__main__":
    main()
