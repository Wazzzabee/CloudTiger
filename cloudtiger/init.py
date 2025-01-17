""" Initial operations needed for CloudTiger """
from genericpath import exists
import json
import os
import shutil
import subprocess
import sys

import click
import base64
import netaddr
import yaml

from cloudtiger.cloudtiger import Operation
from cloudtiger.common_tools import load_yaml, j2, create_ssh_keys, read_user_choice, get_credentials
from cloudtiger.data import available_infra_services, terraform_vm_resource_name, provider_secrets_helper

def config(operation: Operation):

    """ this function executes the initial configuration of a CloudTiger project folder

    :param operation: Operation, the current Operation
    """

    # root .env file

    root_dotenv = {}

    dotenv_configuration_prompt = """
Let us configure some parameters for your CloudTiger project folder."""
    print(dotenv_configuration_prompt)

    dotenv_asking_private_ssh_key = """
Do you wish to use one of your own private SSH key to access your resources ?"""
    use_private_ssh_key = click.prompt(dotenv_asking_private_ssh_key, default=True, type=click.BOOL)

    if use_private_ssh_key:
        dotenv_private_ssh_key_path = """
Please provide the local path to your private SSH key path"""
        private_ssh_key_path = click.prompt(dotenv_private_ssh_key_path, default="~/.ssh/id_rsa")
        if not os.path.exists(os.path.expanduser(private_ssh_key_path)):
            operation.logger.info("The provided SSH key path %s does not exist, exiting" %
                                  private_ssh_key_path)

        root_dotenv["CLOUDTIGER_PRIVATE_SSH_KEY_PATH"] = private_ssh_key_path

    dotenv_asking_ssh_username = """
Please provide a SSH username to connect to your resources"""
    ssh_username = click.prompt(dotenv_asking_ssh_username)
    root_dotenv["CLOUDTIGER_SSH_USERNAME"] = ssh_username

    dotenv_asking_ssh_password = """
Do you wish to provide your SSH password ? It will be stored locally in the .env file encoded in base64.
If no, you will be prompted to provide your SSH password when executing Ansible """
    store_ssh_password = click.prompt(dotenv_asking_ssh_password, default=True, type=click.BOOL)

    if store_ssh_password:
        dotenv_ssh_password = """
Please provide your SSH password"""
        ssh_password = click.prompt(dotenv_ssh_password, hide_input=True)
        root_dotenv["CLOUDTIGER_SSH_PASSWORD"] = base64.b64encode(bytes(ssh_password, 'utf-8'))

    root_dotenv_content = "\n".join(
        [format("export %s=%s" % (key, value)) for key, value in root_dotenv.items()]) + "\n"
    with open(os.path.join(operation.scope, ".env"), "w") as f:
        f.write(root_dotenv_content)

    # .env file per cloud provider

    provider_dotenv_configuration_prompt = """
Now, let us configure credentials for your chosen cloud provider."""
    print(provider_dotenv_configuration_prompt)

    chosen_provider = read_user_choice("cloud provider", list(terraform_vm_resource_name.keys()))

    get_credentials(
        provider_secrets_helper[chosen_provider],
        os.path.join(operation.scope, "secrets", chosen_provider)
    )

    # check if the user wants to use a Terraform backend

    provider_dotenv_tf_backend_prompt = """
Do you wish to use a remote Terraform backend ? If yes, you will need to provide them beforehand"""
    use_tf_backend = click.prompt(provider_dotenv_tf_backend_prompt, default=False, type=click.BOOL)

    if use_tf_backend:
        get_credentials(provider_secrets_helper["tf_backend"],
                        os.path.join(operation.scope, "secrets", chosen_provider),
                        append=True)

def folder(operation: Operation):

    """ this function creates a bootstrap project root folder ('gitops')
    for CloudTiger

    :param operation: Operation, the current Operation
    """

    os.makedirs(operation.scope, exist_ok=True)
    gitops_template = os.path.join(operation.libraries_path, "internal", "gitops")
    shutil.copytree(gitops_template, operation.scope, dirs_exist_ok=True)


def set_ssh_keys(operation: Operation):

    """ this function creates a dedicated pair of SSH keys for the scope if needed
    by the config.yml

    :param operation: Operation, the current Operation
    """

    private_ssh_folder = ""
    public_ssh_folder = ""
    ssh_key_name = ""

    # first option, dedicated ssh keys pair wanted
    if operation.scope_config_dict.get("dedicated_ssh_keys", False):
        private_ssh_folder = os.path.join(operation.project_root, "secrets", "ssh",
                                          operation.scope, "private")
        public_ssh_folder = os.path.join(operation.project_root, "secrets", "ssh",
                                         operation.scope, "public")
        ssh_key_name = operation.scope_config_dict.get("ssh_key_name", 
                                                       operation.scope.replace(os.sep, "_"))

        create_ssh_keys(operation.logger, private_ssh_folder, public_ssh_folder,
                        ssh_key_name=ssh_key_name)

    else:
        # second option, we use the CLOUDTIGER_SSH_KEY_PATH only
        if "CLOUDTIGER_PRIVATE_SSH_KEY_PATH" not in os.environ.keys():
            sys.exit("The environment variable CLOUDTIGER_PRIVATE_SSH_KEY_PATH is not set, exiting")
        private_ssh_key_path = os.environ.get("CLOUDTIGER_PRIVATE_SSH_KEY_PATH")
        private_ssh_key_path = os.path.expanduser(private_ssh_key_path)

        if not os.path.exists(private_ssh_key_path):
            sys.exit("The provided private SSH key does not exist in this path, exiting")

        # private key already exists, we return
        operation.logger.info("The private SSH key %s does exist, going forward"
                              % private_ssh_key_path)

    return


def configure_ip(operation: Operation):

    """ this function generates the config_ips.yml file associated with the scope

    :param operation: Operation, the current Operation
    """

    # listing all the subnets that need to be crawled for available IPs

    subnets_to_crawl = {}
    for network_name, network_subnets in operation.scope_config_dict.get('vm', {}).items():
        for subnet_name, subnet_vms in network_subnets.items():
            subnets_to_crawl[network_name] = []
            for vm_name, vm in subnet_vms.items():
                has_subnet_managed_ips = operation.scope_config_dict["network"][network_name]\
                    ["subnets"][subnet_name].get("managed_ips", False)
                if has_subnet_managed_ips & ("private_ip" not in vm.keys()):
                    subnets_to_crawl[network_name].append(subnet_name)
                    break

    # we 'fping' the subnets to find available IPs
    available_ips = {}
    for network_name, network_subnets in subnets_to_crawl.items():
        available_ips[network_name] = {}
        for subnet_name in network_subnets:
            command = format("fping -g %s" % (operation.scope_config_dict["network"][network_name]\
                ["subnets"][subnet_name]["cidr_block"]))
            operation.logger.info("Executing command %s" % command)
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            # out, err = process.communicate()
            process.wait()
            out = process.stdout.read()
            ips = out.split('\n')
            all_available_ips = [
                x.split(' ')[0]
                for x in ips if x.split(' ')[-1] == "unreachable"
            ]

            # in order to avoid broadcast IPs
            all_available_ips.reverse()

            # in order to avoid gateway IP
            all_available_ips.pop()

            # we get the list of forbidden IPs
            forbidden_range_start = operation.scope_config_dict["network"][network_name]\
                ["subnets"][subnet_name].get("forbidden_range_start", None)
            forbidden_range_stop = operation.scope_config_dict["network"][network_name]\
                ["subnets"][subnet_name].get("forbidden_range_stop", None)
            if (forbidden_range_start is not None) & (forbidden_range_stop is not None):
                forbidden_cidr_block = netaddr.iter_iprange(forbidden_range_start,
                                                            forbidden_range_stop)
                forbiddend_addresses_pool = [str(addr) for addr in forbidden_cidr_block]
            else:
                forbiddend_addresses_pool = []

            available_ips[network_name][subnet_name] = [
                ip for ip in all_available_ips if ip not in forbiddend_addresses_pool
                ]

    # we load the IPs already set for the current scope
    operation.set_terraform_output_info()

    # we update the config_ips using the available IPs for the VMs missing an attributed IP
    updated_config_ip = {
        network_name: {
            subnet_name: {
                "addresses": {
                    vm_name: vm.get("private_ip", 
                                    operation.terraform_vm_data\
                                        .get(
                                            vm_name,
                                            {"private_ip":"not_learned_yet"})["private_ip"])
                    for vm_name, vm in subnet_vms.items()
                }
            }
            for subnet_name, subnet_vms in network_subnets.items()
        }
        for network_name, network_subnets in operation.scope_config_dict.get("vm", {}).items()
    }

    for network_name, network_subnets in updated_config_ip.items():
        for subnet_name, subnet_vms in network_subnets.items():
            for vm_name, address in subnet_vms["addresses"].items():
                if address == "not_learned_yet":
                    has_subnet_managed_ips = operation.scope_config_dict["network"][network_name]\
                        ["subnets"][subnet_name].get("managed_ips", False)
                    if has_subnet_managed_ips:
                        updated_config_ip[network_name][subnet_name]["addresses"][vm_name] = \
                            available_ips[network_name][subnet_name].pop()
                    else:
                        updated_config_ip[network_name][subnet_name]["addresses"][vm_name] = \
                            "not_learned_yet"

    scope_ips = os.path.join(operation.scope_config_folder, 'config_ips.yml')
    with open(scope_ips, 'w') as f:
        yaml.dump(updated_config_ip, f)


def prepare_scope_folder(operation: Operation):

    """ this function creates the <GITOPS>/scopes/<SCOPE> folder associated
    with your scope

    :param operation: Operation, the current Operation
    """

    # create scope folder
    operation.logger.info("Creating scope folder %s" % operation.scope_folder)
    os.makedirs(operation.scope_terraform_folder, exist_ok=True)

    # copying standard terraform folder
    template_folder = os.path.join(operation.libraries_path, "internal", "terraform_providers")
    operation.logger.debug("Creating scope from terraform template folder : %s" % template_folder)
    shutil.copytree(template_folder, operation.scope_terraform_folder, dirs_exist_ok=True)

    # copying needed provider's modules into project root
    operation.logger.debug("Creating Terraform modules folder from libraries folder : %s"
                          % operation.libraries_path)
    tf_modules = os.path.join(
        operation.libraries_path, "terraform", "providers", operation.provider)
    target_modules = os.path.join(
        operation.project_root, "terraform", "providers", operation.provider)
    os.makedirs(target_modules, exist_ok=True)
    shutil.copytree(tf_modules, target_modules, dirs_exist_ok=True)

    # loading attributed IPs from config_ips.yml
    operation.load_ips()

    # setting terraform files from jinja templates
    operation.logger.debug("setting services parameters for scope %s" % operation.scope)

    for service in available_infra_services:
        if service in operation.used_services:
            j2(operation.logger, os.path.join(operation.scope_folder,
                                              "terraform", "services", service + ".tfvars.j2"),
               operation.scope_config_dict,
               os.path.join(operation.scope_folder, "terraform", "services", service + ".tfvars"))
        os.remove(os.path.join(operation.scope_folder, "terraform", "services",
                               service + ".tfvars.j2"))

    for tf_file in ["outputs.tf", "modules.tf", "provider.tf", "terraform.tfvars"]:
        tf_template_path = os.path.join(operation.scope_folder, "terraform", tf_file + ".j2")
        tf_file_path = os.path.join(operation.project_root, "scopes",
                                    operation.scope, "terraform", tf_file)
        j2(operation.logger, tf_template_path, operation.scope_config_dict, tf_file_path)
        os.remove(tf_template_path)

    for yaml_file in ["firewall_standard", "vm_standard", "disk_standard"]:
        yaml_file_path = os.path.join(operation.libraries_path, "internal", "standard",
                                      yaml_file + ".yml")
        tf_file_dest = os.path.join(operation.project_root, "scopes", operation.scope,
                                    "terraform", yaml_file + ".auto.tfvars.json")
        yaml_file_content = load_yaml(operation.logger, yaml_file_path)
        # we supercharge the vm_standard file with extra entries from
        # <GITOPS_FOLDER>/standard/standard.yml
        if yaml_file == "vm_standard":
            yaml_file_content = operation.standard_config
        with open(tf_file_dest, "w") as f:
            json.dump(yaml_file_content, f, indent=4)

    operation.logger.info("Successfully created and set scope folder")


def prepare_platform_action(
        operation: Operation,
        platform: dict,
        platform_parent_folder: str,
        platform_common_values: dict,
        addresses_pool_offset: int
        ):

    """ this function executes the action needed by a level of a platform description

    :param operation: Operation, the current Operation
    :param platform: dict, the dictionary of parameters for the current scope and subscopes
    :param platform_parent_folder: str, the parent folder of the current scope
    :param platform_common_values: dict, the dictionary of parameters shared by all subscopes
    :param addresses_pool_offset: int, the offset in the list of available IPs
    (below the offset = used IPs)
    """

    # the meta_config defines a IP addresses pool, from which we draw IP addresses for VMs
    # in successive scopes. It means we have to keep track of an offset of already attributed
    # addresses from the pool

    os.makedirs(platform_parent_folder, exist_ok=True)

    # we set the offset in the addresses pool
    platform_common_values["addresses_pool_offset"] = addresses_pool_offset

    # we prepare a config.yaml from jinja template
    subfolder_values = dict(platform_common_values, **platform)
    subfolder_values = dict(subfolder_values, **(operation.standard_config))
    subfolder_values["vm_class"] = "nonprod"
    if os.sep + 'prod' in platform_parent_folder:
        subfolder_values["vm_class"] = "prod"

    environment = platform_common_values.get("environment", "")
    if environment != "":
        environment += "_"

    operation.logger.info("Creating subscope %s" % platform_parent_folder)

    subfolder_network_name = list(subfolder_values["network"].keys())[0]
    subfolder_network = subfolder_values["network"][subfolder_network_name]

    subfolder_subnet_name = list(subfolder_network["subnets"].keys())[0]
    subfolder_subnet = subfolder_network["subnets"][subfolder_subnet_name]

    subconfig = {
        "network": subfolder_values.get("network", {}),
        "kubernetes": subfolder_values.get("kubernetes", {}),
        "policies": subfolder_values.get("policies", {}),
        "provider": subfolder_values.get("provider", {}),
        "vm": {
            subfolder_network_name: {
                subfolder_subnet_name: {
                    vm.get("vm_prefix", subfolder_values["vm_prefix"]) + environment
                    + vm["type"] + vm.get("indice", "") + "."
                    + subfolder_values["client_name"]: {
                        "availability_zone": vm.get("availability_zone",
                                                    subfolder_subnet["availability_zone"]),
                        "data_volume_size": vm.get("data_volume_size", 
                                                   operation.standard_config["vm_types"]\
                                                       [operation.vm_type_provider]\
                                                           [vm["type"]]\
                                                               [subfolder_values["vm_class"]]\
                                                                   ["data_volume_size"]),
                        "group": vm["type"],
                        "private_ip": subfolder_values["addresses_pool"]\
                            [addresses_pool_offset + subfolder_values.get("vms", []).index(vm)],
                        "root_volume_size": subfolder_values["root_volume_size"]\
                            .get(subfolder_values["provider"], 32),
                        "system_image": vm.get("system_image",
                                               operation.standard_config["vm_types"]\
                            [operation.vm_type_provider][vm["type"]][subfolder_values["vm_class"]]\
                                .get("system_image", subfolder_values["default_os_images"]\
                                    [operation.vm_type_provider])),
                        "size": {
                            "memory": vm.get("memory", operation.standard_config["vm_types"]\
                                [operation.vm_type_provider][vm["type"]]\
                                    [subfolder_values["vm_class"]]["memory"]),
                            "nb_sockets": vm.get("nb_sockets",
                                                 operation.standard_config["vm_types"]\
                                [operation.vm_type_provider][vm["type"]]\
                                    [subfolder_values["vm_class"]]["nb_sockets"]),
                            "nb_vcpu_per_socket": vm.get("nb_sockets",
                                                         operation.standard_config["vm_types"]\
                                                             [operation.vm_type_provider]\
                                                                 [vm["type"]]\
                                                                     [subfolder_values["vm_class"]]\
                                                                     ["nb_vcpu_per_socket"])
                        }
                    } for vm in subfolder_values.get("vms", [])
                }
            }
        }
    }

    if platform_common_values.get("use_tf_backend", False):
        subconfig["use_tf_backend"] = True

    with open(os.path.join(platform_parent_folder, "config.yml"), "w") as f:
        yaml.dump(subconfig, f)

    # we update the offset in the addresses pool
    nb_vms_in_subfolder = len(subfolder_values.get("vms", []))
    addresses_pool_offset += nb_vms_in_subfolder

    # we do the same for subscopes
    for key, val in platform.items():
        if key not in ['kubernetes', "spark_cluster", "environments", "vms"]:
            if isinstance(val, dict):
                new_platform_folder = os.path.join(platform_parent_folder, key)
                # print(key)
                if key in ["preprod", "pprod", "prod", "production"]:
                    platform_common_values["environment"] = key
                    # print(val["environment"])
                addresses_pool_offset = prepare_platform_action(
                    operation,
                    val,
                    new_platform_folder,
                    platform_common_values,
                    addresses_pool_offset
                    )

    return addresses_pool_offset


def init_meta_distribute(operation: Operation):

    """ this function generates the folders and configuration tree for a full platform
    for a dedicated customer

    :param operation: Operation, the current Operation
    """

    # we check if the meta_config already exists
    meta_config = {'infra':dict()}
    meta_config_path = os.path.join(operation.scope_config_folder, "meta_config.yml")
    if os.path.isfile(meta_config_path):
        with open(meta_config_path, "r") as f:
            try:
                meta_config = yaml.load(f, Loader=yaml.FullLoader)
            except Exception as e:
                operation.logger.error("Failed to open meta_config file % with error %s".format(meta_config_path, e))

    # we set the addresses pool
    # cidr_block = ipaddress.summarize_address_range(
    # ipaddress.IPv4Address(meta_config["addresses_pool_start"]),
    # ipaddress.IPv4Address(meta_config["addresses_pool_end"]))
    # addresses_pool = [str(addr) for addr in cidr_block]
    addresses_pool = []
    if "addresses_pool" in meta_config.keys():
        if isinstance(meta_config["addresses_pool"], list):
            addresses_pool = meta_config["addresses_pool"]
    else:
        if ("addresses_pool_start" in meta_config.keys()) &\
        ("addresses_pool_end" in meta_config.keys()):
            cidr_block = netaddr.iter_iprange(meta_config["addresses_pool_start"],
                                              meta_config["addresses_pool_end"])
            addresses_pool = [str(addr) for addr in cidr_block]
        else:
            operation.logger.error("Addresses pool not provided or badly formatted, exiting")
            sys.exit()

    operation.logger.debug("Working with addresses pool : %s" % addresses_pool)
    meta_config["addresses_pool"] = addresses_pool
    addresses_pool_offset = 0

    # we loop through the folders requested by the meta_config to create subscopes
    prepare_platform_action(operation, meta_config['infra'], operation.scope_config_folder,
                            meta_config, addresses_pool_offset)


def init_meta_aggregate(operation: Operation):

    """ this function aggregates all config files in current folder and subfolders
    into a meta_config.yml

    :param operation: Operation, the current Operation
    """

    # we loop through the config folder
    meta_config = {'ansible':dict()}
    for (root, _, files) in os.walk(operation.scope_config_folder):
        if 'config.yml' in files:
            subconfig_path = os.path.join(root, 'config.yml')
            subconfig_scope = os.path.join(*(subconfig_path.split(os.sep)\
                [len(operation.scope_config_folder.split(os.sep)):-1]))
            # print(subconfig_scope)
            with open(subconfig_path, 'r') as f:
                try:
                    subconfig_data = yaml.load(f, Loader=yaml.FullLoader)
                except Exception as e:
                    operation.logger.error(
                        "Failed to open file % with error %s".format(subconfig_path, e))
            meta_config['ansible'][subconfig_scope] = subconfig_data.get('ansible')

    # we write the meta_config.yml
    meta_config_path = os.path.join(operation.scope_config_folder, "meta_config.yml")
    with open(meta_config_path, "w") as f:
        yaml.dump(meta_config, f)
