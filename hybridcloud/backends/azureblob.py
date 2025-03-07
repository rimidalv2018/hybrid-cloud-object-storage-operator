import string
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.storage.models import StorageAccountCreateParameters, StorageAccountUpdateParameters, Sku, \
    BlobServiceProperties, \
    CorsRules, CorsRule, NetworkRuleSet, IPRule, VirtualNetworkRule, BlobContainer, \
    StorageAccountCheckNameAvailabilityParameters, StorageAccountRegenerateKeyParameters, \
    DeleteRetentionPolicy, RestorePolicyProperties, ChangeFeed, LocalUser, PermissionScope, SshPublicKey
from azure.mgmt.resource.locks.models import ManagementLockObject
from ..util.azure import azure_client_storage, azure_client_locks
from ..config import config_get
from ..util.reconcile_helpers import field_from_spec

TAGS_PREFIX = "hybridcloud-object-storage-operator"
HTTP_METHODS = ["DELETE", "GET", "HEAD", "MERGE", "OPTIONS", "PATCH", "POST", "PUT"]
SFTP_USER_PERMISSIONS = ["READ", "WRITE", "DELETE", "LIST", "CREATE"]


def _backend_config(key, default=None, fail_if_missing=False):
    return config_get(f"backends.azureblob.{key}", default=default, fail_if_missing=fail_if_missing)


def _calc_name(namespace, name):
    # Allow admins to override names so that existing storage accounts not following the schema can still be managed
    name_overrides = _backend_config("name_overrides", default=[])
    for override in name_overrides:
        if override["namespace"] == namespace and override["name"] == name:
            return override["azure_name"]
    # Use name pattern to calculate name
    calculated_name = _backend_config("name_pattern", fail_if_missing=True).format(namespace=namespace, name=name).lower()
    # Azure requires storage account names to only consist of numbers and lowercase letters
    return ''.join(filter(lambda el: el in string.ascii_lowercase+string.digits, calculated_name))


class AzureBlobBackend:
    def __init__(self, logger):
        self._logger = logger
        self._storage_client = azure_client_storage()
        self._lock_client = azure_client_locks()
        self._subscription_id = _backend_config("subscription_id", fail_if_missing=True)
        self._location = _backend_config("location", fail_if_missing=True)
        self._resource_group = _backend_config("resource_group", fail_if_missing=True)

    def bucket_spec_valid(self, namespace, name, spec):
        bucket_name = _calc_name(namespace, name)
        if len(bucket_name) > 24:
            return (False, f"calculated storage account name '{bucket_name}' is longer than 24 characters")
        if not self.bucket_exists(namespace, name):
            result = self._storage_client.storage_accounts.check_name_availability(StorageAccountCheckNameAvailabilityParameters(name=bucket_name))
            if not result.name_available:
                return (False, f"storage account name cannot be used: {result.reason}: {result.message}")

        # Check user permissions validity
        spec_user_permissions = _get_user_permissions(spec)
        for permission in spec_user_permissions:
            if permission not in SFTP_USER_PERMISSIONS:
                return (False, f"user permission '{permission}' is not valid")
        # Check bucket containers and containers used by local users
        spec_user_containers = _get_user_container_names(spec)
        spec_containers = _get_container_names(spec)
        for container in spec_user_containers:
            if container not in spec_containers:
                return (False, f"user container name '{container}' is not defined in the general container list")

        sftp_enabled = field_from_spec(spec, "sftp.enabled", default=_backend_config("parameters.sftp.enabled",
                                                                             default=False))
        # Check if existing bucket can use enabled SFTP option
        if self.bucket_exists(namespace, name):
            storage_account = self._storage_client.storage_accounts.get_properties(self._resource_group, bucket_name)
            is_hns_enabled_in_storage_account = storage_account.is_hns_enabled
            # if hierarchical namespace (HNS) not aénabled at creation, SFTP cannot be enabled
            if sftp_enabled and not is_hns_enabled_in_storage_account:
                return (False, f"SFTP cannot be enabled because hierarchical namespace (HNS) option is disabled. SFTP can be enabled only at creation time of the storage account")

        # Check if SFTP and Versioning are both enabled; this isn't a valid state
        versioning = field_from_spec(spec, "dataRetention.versioning.enabled", default=_backend_config("parameters.versioning.enabled", default=False))
        if sftp_enabled and versioning:
            return (False, "SFTP and Versioning options cannot be both enabled")

        return (True, "")

    def bucket_exists(self, namespace, name):
        bucket_name = _calc_name(namespace, name)
        try:
            return self._storage_client.storage_accounts.get_properties(self._resource_group, bucket_name)
        except ResourceNotFoundError:
            return False

    def create_or_update_bucket(self, namespace, name, spec):
        bucket_name = _calc_name(namespace, name)
        sku = Sku(name=_backend_config("sku.name", default="Standard_LRS"))
        public_access = field_from_spec(spec, "network.publicAccess", default=_backend_config("parameters.network.public_access", default=False))
        network_rules = self._map_network_rules(spec, public_access)
        tags = _calc_tags(namespace, name)
        sftp_enabled = field_from_spec(spec, "sftp.enabled", default=_backend_config("parameters.sftp.enabled",
                                                                                     default=False))
        try:
            storage_account = self._storage_client.storage_accounts.get_properties(self._resource_group, bucket_name)
        except:
            storage_account = None

        if not storage_account:
            # Create storage account
            parameters = StorageAccountCreateParameters(
                sku=sku,
                kind=_backend_config("kind", default="StorageV2"),
                location=self._location,
                tags=tags,
                public_network_access="Enabled",  # Disabled means only via endpoint connection,
                network_rule_set=network_rules,
                access_tier=_backend_config("access_tier", default="Hot"),
                enable_https_traffic_only=True,
                allow_blob_public_access=_backend_config("allow_anonymous_access", default=False),
                allow_shared_key_access=True,
                is_sftp_enabled=sftp_enabled,
                # needed to enable sftp
                is_hns_enabled=sftp_enabled,
                is_local_user_enabled=sftp_enabled
            )
            self._storage_client.storage_accounts.begin_create(self._resource_group, bucket_name, parameters=parameters).result()
        else:
            # Update storage account
            parameters = StorageAccountUpdateParameters(
                tags=tags,
                public_network_access="Enabled",  # Disabled means only via endpoint connection
                network_rule_set=network_rules,
                is_sftp_enabled=sftp_enabled,
                # needed to enable sftp
                is_local_user_enabled=sftp_enabled
            )
            self._storage_client.storage_accounts.update(self._resource_group, bucket_name, parameters=parameters)

        if _backend_config("lock_from_deletion", default=False):
            self._lock_client.management_locks.create_or_update_at_resource_level(self._resource_group, "Microsoft.Storage", "", "storageAccounts", bucket_name, "DoNotDeleteLock", parameters=ManagementLockObject(level="CanNotDelete", notes="Protection from accidental deletion"))

        # Create blob services
        retention, changefeed = _map_retention(spec)
        versioning = field_from_spec(spec, "dataRetention.versioning.enabled", default=_backend_config("parameters.versioning.enabled", default=False))
        parameters = BlobServiceProperties(
            cors=_map_cors_rules(spec.get("security", dict()).get("cors")),
            is_versioning_enabled=versioning,
            delete_retention_policy=retention,
            container_delete_retention_policy=retention,
            restore_policy=None,
            change_feed=changefeed,
        )
        self._storage_client.blob_services.set_service_properties(self._resource_group, bucket_name, parameters=parameters)

        # Create containers
        existing_containers = dict()
        for container in self._storage_client.blob_containers.list(self._resource_group, bucket_name):
            existing_containers[container.name] = container
        for container in spec.get("containers", []):
            public_access = "Blob" if container.get("anonymousAccess", False) else "None"
            parameters = BlobContainer(public_access=public_access)
            existing_container = existing_containers.pop(container["name"]) if container["name"] in existing_containers else None
            if not existing_container:
                self._storage_client.blob_containers.create(self._resource_group, bucket_name, container["name"], blob_container=parameters)
            elif existing_container.public_access != public_access:
                self._storage_client.blob_containers.update(self._resource_group, bucket_name, container["name"], blob_container=parameters)
        for container in existing_containers.values():
            if container.name.startswith("$"):
                # system containers, ignore
                continue
            self._storage_client.blob_containers.delete(self._resource_group, bucket_name, container.name)
        # local users for SFTP
        if sftp_enabled:
            for user in field_from_spec(spec, "sftp.users", default=[]):
                sftp_username = user["username"]
                # get local user containers
                user_permission_scopes = _get_user_permission_scopes(user)
                # get local user authorized keys
                user_authorized_keys = _get_user_authorized_keys(user)
                local_user_properties = LocalUser(permission_scopes=user_permission_scopes,
                                                  ssh_authorized_keys=user_authorized_keys)
                # Create user
                self._storage_client.local_users.create_or_update(self._resource_group, bucket_name,
                                                                  sftp_username, properties=local_user_properties)
            # Delete users
            users_from_spec = []
            for user in field_from_spec(spec, "sftp.users", default=[]):
                users_from_spec.append(user["username"])
            for user in self._storage_client.local_users.list(self._resource_group, bucket_name):
                existing_username = user.name
                if existing_username not in users_from_spec:
                    self._storage_client.local_users.delete(self._resource_group, bucket_name, existing_username)

        # Credentials
        for key in self._storage_client.storage_accounts.list_keys(self._resource_group, bucket_name).keys:
            if key.key_name == "key1":
                return {
                    "interface": "azureblob",
                    "endpoint": f"https://{bucket_name}.blob.core.windows.net",
                    "key": key.value,
                    "connection_string": f"DefaultEndpointsProtocol=https;AccountName={bucket_name};AccountKey={key.value};EndpointSuffix=core.windows.net",
                }
        raise Exception("Could not find keys in azure")


    def delete_bucket(self, namespace, name):
        bucket_name = _calc_name(namespace, name)
        delete_fake = _backend_config("delete_fake", default=False)
        if delete_fake:
            tags = _calc_tags(namespace, name, {"marked-for-deletion": "yes"})
            self._storage_client.storage_accounts.update(self._resource_group, bucket_name, parameters=StorageAccountUpdateParameters(tags=tags))
        else:
            self._storage_client.storage_accounts.delete(self._resource_group, bucket_name)

    def reset_credentials(self, namespace, name):
        bucket_name = _calc_name(namespace, name)
        self._storage_client.storage_accounts.regenerate_key(self._resource_group, bucket_name, StorageAccountRegenerateKeyParameters(key_name="key1"))
        # Credentials
        for key in self._storage_client.storage_accounts.list_keys(self._resource_group, bucket_name).keys:
            if key.key_name == "key1":
                return {
                    "interface": "azureblob",
                    "endpoint": f"https://{bucket_name}.blob.core.windows.net",
                    "key": key.value,
                    "connection_string": f"DefaultEndpointsProtocol=https;AccountName={bucket_name};AccountKey={key.value};EndpointSuffix=core.windows.net",
                }
        raise Exception("Could not find keys in azure")

    def _map_network_rules(self, spec, public_access):
        ip_rules = []
        spec_rules = field_from_spec(spec, "network.firewallRules", [])
        extra_rules = _backend_config("parameters.network.firewall_rules", default=[])
        for rule in spec_rules + extra_rules:
            ip_rules.append(IPRule(ip_address_or_range=rule["cidr"], action="Allow"))
        virtual_network_rules = []
        for config in _backend_config("network.vnets", default=[]):
            vnet = config["vnet"]
            subnet = config["subnet"]
            resource_id = f"/subscriptions/{self._subscription_id}/resourceGroups/{self._resource_group}/providers/Microsoft.Network/virtualNetworks/{vnet}/subnets/{subnet}"
            virtual_network_rules.append(VirtualNetworkRule(virtual_network_resource_id=resource_id, action="Allow"))
        bypass = "AzureServices" if _backend_config("network.allow_azure_services", default=False) else None
        return NetworkRuleSet(
            bypass=bypass,
            resource_access_rules=[],
            virtual_network_rules=virtual_network_rules,
            ip_rules=ip_rules,
            default_action="Allow" if public_access else "Deny"
        )


def _map_cors_rules(cors):
    if not cors:
        return None
    rules = []
    for rule in cors:
        rules.append(CorsRule(
            allowed_origins=rule["allowedOrigins"],
            allowed_methods=list(filter(lambda el: el in HTTP_METHODS, rule["allowedMethods"])),
            max_age_in_seconds=int(rule["maxAgeInSeconds"]),
            exposed_headers=rule["exposedHeaders"],
            allowed_headers=rule["allowedHeaders"]
        ))
    return CorsRules(cors_rules=rules)


def _map_retention(spec):
    enabled = field_from_spec(spec, "dataRetention.deleteRetention.enabled", default=_backend_config("parameters.delete_retention.enabled", default=False))
    days = field_from_spec(spec, "dataRetention.deleteRetention.retentionPeriodInDays", default=_backend_config("parameters.delete_retention.days", default=1))
    retention = DeleteRetentionPolicy(
        enabled=enabled,
        days=days if enabled else None,
    )
    changefeed = ChangeFeed(
        enabled=False,
        retention_in_days=None,
    )
    return retention, changefeed


def _calc_tags(namespace, name, extra_tags={}):
    tags = {f"{TAGS_PREFIX}:namespace": namespace, f"{TAGS_PREFIX}:name": name}
    for k, v in extra_tags.items():
        tags[f"{TAGS_PREFIX}:{k}"] = v
    for k, v in _backend_config("tags", default={}).items():
        tags[k] = v.format(namespace=namespace, name=name)
    return tags


def _map_user_permissions(spec_permissions):
    mapped_user_permissions = ''
    valid_permissions = list(filter(lambda el: el in SFTP_USER_PERMISSIONS, spec_permissions))
    for valid_permission in valid_permissions:
        mapped_user_permissions += _map_user_permission(valid_permission)
    return mapped_user_permissions


def _map_user_permission(spec_permission):
    match spec_permission:
        case "READ":
            return 'r'
        case "WRITE":
            return 'w'
        case "DELETE":
            return 'd'
        case "LIST":
            return 'l'
        case "CREATE":
            return 'c'
        case _:
            return ''


def _get_user_permission_scopes(user):
    user_permission_scopes = []
    for user_access in user.get("access", []):
        user_resource_name = user_access.get("container", "")
        spec_permissions = user_access.get("permissions", [])
        if (not user_resource_name) or (not spec_permissions):
            continue
        user_permission_scope = _map_user_permissions(spec_permissions)
        user_service = "blob"
        permission_scope = PermissionScope(
            permissions=user_permission_scope,
            service=user_service,
            resource_name=user_resource_name
        )
        user_permission_scopes.append(permission_scope)
    return user_permission_scopes


def _get_user_access_entries(spec):
    user_access_entries = []
    for user in field_from_spec(spec, "sftp.users", default=[]):
        for access in user.get("access", []):
            user_access_entries.append(access)
    return user_access_entries


def _get_user_permissions(spec):
    user_permissions = []
    for access in _get_user_access_entries(spec):
        for permission in access.get("permissions", []):
            user_permissions.append(permission)
    return user_permissions


def _get_user_container_names(spec):
    user_containers = []
    for access in _get_user_access_entries(spec):
        if "container" in access:
            user_containers.append(access["container"])
    return user_containers


def _get_container_names(spec):
    container_names = []
    for container in spec.get("containers", []):
        if "name" in container:
            container_names.append(container["name"])
    return container_names


def _get_user_authorized_keys(user):
    user_authorized_keys = []
    for user_ssh_key in user.get("sshKeys", []):
        user_key_description = user_ssh_key.get("description", "")
        user_public_ssh_key = user_ssh_key.get("publicKey")
        ssh_public_key = SshPublicKey(description=user_key_description, key=user_public_ssh_key)
        user_authorized_keys.append(ssh_public_key)
    return user_authorized_keys
