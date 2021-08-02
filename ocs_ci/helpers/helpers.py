"""
Helper functions file for OCS QE
"""
import base64
import random
import datetime
import hashlib
import json
import logging
import os
import re
import statistics
import tempfile
import threading
import time
import inspect
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from subprocess import PIPE, TimeoutExpired, run
from uuid import uuid4

import yaml

from ocs_ci.framework import config
from ocs_ci.helpers.proxy import (
    get_cluster_proxies,
    update_container_with_proxy_env,
)
from ocs_ci.ocs.utils import mirror_image
from ocs_ci.ocs import constants, defaults, node, ocp
from ocs_ci.ocs.exceptions import (
    CommandFailed,
    ResourceWrongStatusException,
    TimeoutExpiredError,
    UnavailableBuildException,
    UnexpectedBehaviour,
)
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.resources import pod, pvc
from ocs_ci.ocs.resources.ocs import OCS
from ocs_ci.utility import templating
from ocs_ci.utility.retry import retry
from ocs_ci.utility.utils import (
    TimeoutSampler,
    ocsci_log_path,
    run_cmd,
    update_container_with_mirrored_image,
)

logger = logging.getLogger(__name__)
DATE_TIME_FORMAT = "%Y I%m%d %H:%M:%S.%f"


def create_unique_resource_name(resource_description, resource_type):
    """
    Creates a unique object name by using the object_description,
    object_type and a random uuid(in hex) as suffix trimmed due to
    kubernetes limitation of 63 characters

    Args:
        resource_description (str): The user provided object description
        resource_type (str): The type of object for which the unique name
            will be created. For example: project, pvc, etc

    Returns:
        str: A unique name
    """
    name = f"{resource_type}-{resource_description[:23]}-{uuid4().hex}"
    return name if len(name) < 40 else name[:40]


def create_resource(do_reload=True, **kwargs):
    """
    Create a resource

    Args:
        do_reload (bool): True for reloading the resource following its creation,
            False otherwise
        kwargs (dict): Dictionary of the OCS resource

    Returns:
        OCS: An OCS instance

    Raises:
        AssertionError: In case of any failure
    """
    ocs_obj = OCS(**kwargs)
    resource_name = kwargs.get("metadata").get("name")
    created_resource = ocs_obj.create(do_reload=do_reload)
    assert created_resource, f"Failed to create resource {resource_name}"
    return ocs_obj


def wait_for_resource_state(resource, state, timeout=60):
    """
    Wait for a resource to get to a given status

    Args:
        resource (OCS obj): The resource object
        state (str): The status to wait for
        timeout (int): Time in seconds to wait

    Raises:
        ResourceWrongStatusException: In case the resource hasn't
            reached the desired state

    """
    if (
        resource.name == constants.DEFAULT_STORAGECLASS_CEPHFS
        or resource.name == constants.DEFAULT_STORAGECLASS_RBD
    ):
        logger.info("Attempt to default default Secret or StorageClass")
        return
    try:
        resource.ocp.wait_for_resource(
            condition=state, resource_name=resource.name, timeout=timeout
        )
    except TimeoutExpiredError:
        logger.error(f"{resource.kind} {resource.name} failed to reach {state}")
        resource.reload()
        raise ResourceWrongStatusException(resource.name, resource.describe())
    logger.info(f"{resource.kind} {resource.name} reached state {state}")


def create_pod(
    interface_type=None,
    pvc_name=None,
    do_reload=True,
    namespace=defaults.ROOK_CLUSTER_NAMESPACE,
    node_name=None,
    pod_dict_path=None,
    sa_name=None,
    dc_deployment=False,
    raw_block_pv=False,
    raw_block_device=constants.RAW_BLOCK_DEVICE,
    replica_count=1,
    pod_name=None,
    node_selector=None,
    command=None,
    command_args=None,
    deploy_pod_status=constants.STATUS_COMPLETED,
):
    """
    Create a pod

    Args:
        interface_type (str): The interface type (CephFS, RBD, etc.)
        pvc_name (str): The PVC that should be attached to the newly created pod
        do_reload (bool): True for reloading the object after creation, False otherwise
        namespace (str): The namespace for the new resource creation
        node_name (str): The name of specific node to schedule the pod
        pod_dict_path (str): YAML path for the pod
        sa_name (str): Serviceaccount name
        dc_deployment (bool): True if creating pod as deploymentconfig
        raw_block_pv (bool): True for creating raw block pv based pod, False otherwise
        raw_block_device (str): raw block device for the pod
        replica_count (int): Replica count for deployment config
        pod_name (str): Name of the pod to create
        node_selector (dict): dict of key-value pair to be used for nodeSelector field
            eg: {'nodetype': 'app-pod'}
        command (list): The command to be executed on the pod
        command_args (list): The arguments to be sent to the command running
            on the pod
        deploy_pod_status (str): Expected status of deploy pod. Applicable
            only if dc_deployment is True

    Returns:
        Pod: A Pod instance

    Raises:
        AssertionError: In case of any failure

    """
    if (
        interface_type == constants.CEPHBLOCKPOOL
        or interface_type == constants.CEPHBLOCKPOOL_THICK
    ):
        pod_dict = pod_dict_path if pod_dict_path else constants.CSI_RBD_POD_YAML
        interface = constants.RBD_INTERFACE
    else:
        pod_dict = pod_dict_path if pod_dict_path else constants.CSI_CEPHFS_POD_YAML
        interface = constants.CEPHFS_INTERFACE
    if dc_deployment:
        pod_dict = pod_dict_path if pod_dict_path else constants.FEDORA_DC_YAML
    pod_data = templating.load_yaml(pod_dict)
    if not pod_name:
        pod_name = create_unique_resource_name(f"test-{interface}", "pod")
    pod_data["metadata"]["name"] = pod_name
    pod_data["metadata"]["namespace"] = namespace
    if dc_deployment:
        pod_data["metadata"]["labels"]["app"] = pod_name
        pod_data["spec"]["template"]["metadata"]["labels"]["name"] = pod_name
        pod_data["spec"]["replicas"] = replica_count

    if pvc_name:
        if dc_deployment:
            pod_data["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"][
                "claimName"
            ] = pvc_name
        else:
            pod_data["spec"]["volumes"][0]["persistentVolumeClaim"][
                "claimName"
            ] = pvc_name

    if interface_type == constants.CEPHBLOCKPOOL and raw_block_pv:
        if pod_dict_path in [constants.FEDORA_DC_YAML, constants.FIO_DC_YAML]:
            temp_dict = [
                {
                    "devicePath": raw_block_device,
                    "name": pod_data.get("spec")
                        .get("template")
                        .get("spec")
                        .get("volumes")[0]
                        .get("name"),
                }
            ]
            if pod_dict_path == constants.FEDORA_DC_YAML:
                del pod_data["spec"]["template"]["spec"]["containers"][0][
                    "volumeMounts"
                ]
                security_context = {"capabilities": {"add": ["SYS_ADMIN"]}}
                pod_data["spec"]["template"]["spec"]["containers"][0][
                    "securityContext"
                ] = security_context

            pod_data["spec"]["template"]["spec"]["containers"][0][
                "volumeDevices"
            ] = temp_dict

        elif pod_dict_path == constants.NGINX_POD_YAML:
            temp_dict = [
                {
                    "devicePath": raw_block_device,
                    "name": pod_data.get("spec")
                        .get("containers")[0]
                        .get("volumeMounts")[0]
                        .get("name"),
                }
            ]
            del pod_data["spec"]["containers"][0]["volumeMounts"]
            pod_data["spec"]["containers"][0]["volumeDevices"] = temp_dict
        else:
            pod_data["spec"]["containers"][0]["volumeDevices"][0][
                "devicePath"
            ] = raw_block_device
            pod_data["spec"]["containers"][0]["volumeDevices"][0]["name"] = (
                pod_data.get("spec").get("volumes")[0].get("name")
            )

    if command:
        if dc_deployment:
            pod_data["spec"]["template"]["spec"]["containers"][0]["command"] = command
        else:
            pod_data["spec"]["containers"][0]["command"] = command
    if command_args:
        if dc_deployment:
            pod_data["spec"]["template"]["spec"]["containers"][0]["args"] = command_args
        else:
            pod_data["spec"]["containers"][0]["args"] = command_args

    if node_name:
        if dc_deployment:
            pod_data["spec"]["template"]["spec"]["nodeName"] = node_name
        else:
            pod_data["spec"]["nodeName"] = node_name

    if node_selector:
        if dc_deployment:
            pod_data["spec"]["template"]["spec"]["nodeSelector"] = node_selector
        else:
            pod_data["spec"]["nodeSelector"] = node_selector

    if sa_name and dc_deployment:
        pod_data["spec"]["template"]["spec"]["serviceAccountName"] = sa_name

    # overwrite used image (required for disconnected installation)
    update_container_with_mirrored_image(pod_data)

    # configure http[s]_proxy env variable, if required
    update_container_with_proxy_env(pod_data)

    if dc_deployment:
        ocs_obj = create_resource(**pod_data)
        logger.info(ocs_obj.name)
        assert (ocp.OCP(kind="pod", namespace=namespace)).wait_for_resource(
            condition=deploy_pod_status,
            resource_name=pod_name + "-1-deploy",
            resource_count=0,
            timeout=360,
            sleep=3,
        )
        dpod_list = pod.get_all_pods(namespace=namespace)
        for dpod in dpod_list:
            if "-1-deploy" not in dpod.name:
                if pod_name in dpod.name:
                    return dpod
    else:
        pod_obj = pod.Pod(**pod_data)
        pod_name = pod_data.get("metadata").get("name")
        logger.info(f"Creating new Pod {pod_name} for test")
        created_resource = pod_obj.create(do_reload=do_reload)
        assert created_resource, f"Failed to create Pod {pod_name}"

        return pod_obj


def create_project(project_name=None):
    """
    Create a project

    Args:
        project_name (str): The name for the new project

    Returns:
        ocs_ci.ocs.ocp.OCP: Project object

    """
    namespace = project_name or create_unique_resource_name("test", "namespace")
    project_obj = ocp.OCP(kind="Project", namespace=namespace)
    assert project_obj.new_project(namespace), f"Failed to create namespace {namespace}"
    return project_obj


def create_multilpe_projects(number_of_project):
    """
    Create one or more projects

    Args:
        number_of_project (int): Number of projects to be created

    Returns:
         list: List of project objects

    """
    project_objs = [create_project() for _ in range(number_of_project)]
    return project_objs


def create_secret(interface_type):
    """
    Create a secret
    ** This method should not be used anymore **
    ** This method is for internal testing only **

    Args:
        interface_type (str): The type of the interface
            (e.g. CephBlockPool, CephFileSystem)

    Returns:
        OCS: An OCS instance for the secret
    """
    secret_data = dict()
    if interface_type == constants.CEPHBLOCKPOOL:
        secret_data = templating.load_yaml(constants.CSI_RBD_SECRET_YAML)
        secret_data["stringData"]["userID"] = constants.ADMIN_USER
        secret_data["stringData"]["userKey"] = get_admin_key()
        interface = constants.RBD_INTERFACE
    elif interface_type == constants.CEPHFILESYSTEM:
        secret_data = templating.load_yaml(constants.CSI_CEPHFS_SECRET_YAML)
        del secret_data["stringData"]["userID"]
        del secret_data["stringData"]["userKey"]
        secret_data["stringData"]["adminID"] = constants.ADMIN_USER
        secret_data["stringData"]["adminKey"] = get_admin_key()
        interface = constants.CEPHFS_INTERFACE
    secret_data["metadata"]["name"] = create_unique_resource_name(
        f"test-{interface}", "secret"
    )
    secret_data["metadata"]["namespace"] = defaults.ROOK_CLUSTER_NAMESPACE

    return create_resource(**secret_data)


def default_ceph_block_pool():
    """
    Returns default CephBlockPool

    Returns:
        default CephBlockPool
    """
    sc_obj = default_storage_class(constants.CEPHBLOCKPOOL)
    cbp_name = sc_obj.get().get("parameters").get("pool")
    return cbp_name if cbp_name else constants.DEFAULT_BLOCKPOOL


def create_ceph_block_pool(
    pool_name=None, replica=3, compression=None, failure_domain=None, verify=True
):
    """
    Create a Ceph block pool
    ** This method should not be used anymore **
    ** This method is for internal testing only **

    Args:
        pool_name (str): The pool name to create
        failure_domain (str): Failure domain name
        verify (bool): True to verify the pool exists after creation,
                       False otherwise
        replica (int): The replica size for a pool
        compression (str): Compression type for a pool

    Returns:
        OCS: An OCS instance for the Ceph block pool
    """
    cbp_data = templating.load_yaml(constants.CEPHBLOCKPOOL_YAML)
    cbp_data["metadata"]["name"] = (
        pool_name if pool_name else create_unique_resource_name("test", "cbp")
    )
    cbp_data["metadata"]["namespace"] = defaults.ROOK_CLUSTER_NAMESPACE
    cbp_data["spec"]["replicated"]["size"] = replica

    cbp_data["spec"]["failureDomain"] = failure_domain or get_failure_domin()

    if compression:
        cbp_data["spec"]["parameters"]["compression_mode"] = compression

    cbp_obj = create_resource(**cbp_data)
    cbp_obj.reload()

    if verify:
        assert verify_block_pool_exists(
            cbp_obj.name
        ), f"Block pool {cbp_obj.name} does not exist"
    return cbp_obj


def create_ceph_file_system(pool_name=None):
    """
    Create a Ceph file system
    ** This method should not be used anymore **
    ** This method is for internal testing only **

    Args:
        pool_name (str): The pool name to create

    Returns:
        OCS: An OCS instance for the Ceph file system
    """
    cfs_data = templating.load_yaml(constants.CEPHFILESYSTEM_YAML)
    cfs_data["metadata"]["name"] = (
        pool_name if pool_name else create_unique_resource_name("test", "cfs")
    )
    cfs_data["metadata"]["namespace"] = defaults.ROOK_CLUSTER_NAMESPACE
    cfs_data = create_resource(**cfs_data)
    cfs_data.reload()

    assert validate_cephfilesystem(
        cfs_data.name
    ), f"File system {cfs_data.name} does not exist"
    return cfs_data


def default_storage_class(
    interface_type,
):
    """
    Return default storage class based on interface_type

    Args:
        interface_type (str): The type of the interface
            (e.g. CephBlockPool, CephFileSystem)

    Returns:
        OCS: Existing StorageClass Instance
    """
    external = config.DEPLOYMENT["external_mode"]
    if interface_type == constants.CEPHBLOCKPOOL:
        if external:
            resource_name = constants.DEFAULT_EXTERNAL_MODE_STORAGECLASS_RBD
        else:
            resource_name = constants.DEFAULT_STORAGECLASS_RBD
        base_sc = OCP(kind="storageclass", resource_name=resource_name)
    elif interface_type == constants.CEPHFILESYSTEM:
        if external:
            resource_name = constants.DEFAULT_EXTERNAL_MODE_STORAGECLASS_CEPHFS
        else:
            resource_name = constants.DEFAULT_STORAGECLASS_CEPHFS
        base_sc = OCP(kind="storageclass", resource_name=resource_name)
    sc = OCS(**base_sc.data)
    return sc


def default_thick_storage_class():
    """
    Return default RBD thick storage class

    Returns:
        OCS: Existing RBD thick StorageClass instance

    """
    external = config.DEPLOYMENT["external_mode"]
    if external:
        resource_name = constants.DEFAULT_EXTERNAL_MODE_STORAGECLASS_RBD_THICK
    else:
        resource_name = constants.DEFAULT_STORAGECLASS_RBD_THICK
    base_sc = OCP(kind="storageclass", resource_name=resource_name)
    sc = OCS(**base_sc.data)
    return sc


def create_storage_class(
    interface_type,
    interface_name,
    secret_name,
    reclaim_policy=constants.RECLAIM_POLICY_DELETE,
    sc_name=None,
    provisioner=None,
    rbd_thick_provision=False,
    encrypted=False,
    encryption_kms_id=None,
):
    """
    Create a storage class
    ** This method should not be used anymore **
    ** This method is for internal testing only **

    Args:
        interface_type (str): The type of the interface
            (e.g. CephBlockPool, CephFileSystem)
        interface_name (str): The name of the interface
        secret_name (str): The name of the secret
        sc_name (str): The name of storage class to create
        reclaim_policy (str): Type of reclaim policy. Defaults to 'Delete'
            (eg., 'Delete', 'Retain')
        rbd_thick_provision (bool): True to enable RBD thick provisioning.
            Applicable if interface_type is CephBlockPool
        encrypted (bool): True to create encrypted SC else False
        encryption_kms_id (str): ID of the KMS entry from connection details

    Returns:
        OCS: An OCS instance for the storage class
    """

    sc_data = dict()
    if interface_type == constants.CEPHBLOCKPOOL:
        sc_data = templating.load_yaml(constants.CSI_RBD_STORAGECLASS_YAML)
        sc_data["parameters"]["csi.storage.k8s.io/node-stage-secret-name"] = secret_name
        sc_data["parameters"][
            "csi.storage.k8s.io/node-stage-secret-namespace"
        ] = defaults.ROOK_CLUSTER_NAMESPACE
        interface = constants.RBD_INTERFACE
        sc_data["provisioner"] = (
            provisioner if provisioner else defaults.RBD_PROVISIONER
        )
        if rbd_thick_provision:
            sc_data["parameters"]["thickProvision"] = "true"
        if encrypted:
            # Avoid circular imports
            from ocs_ci.utility.kms import get_encryption_kmsid

            sc_data["parameters"]["encrypted"] = "true"
            sc_data["parameters"]["encryptionKMSID"] = (
                encryption_kms_id if encryption_kms_id else get_encryption_kmsid()
            )
    elif interface_type == constants.CEPHFILESYSTEM:
        sc_data = templating.load_yaml(constants.CSI_CEPHFS_STORAGECLASS_YAML)
        sc_data["parameters"]["csi.storage.k8s.io/node-stage-secret-name"] = secret_name
        sc_data["parameters"][
            "csi.storage.k8s.io/node-stage-secret-namespace"
        ] = defaults.ROOK_CLUSTER_NAMESPACE
        interface = constants.CEPHFS_INTERFACE
        sc_data["parameters"]["fsName"] = get_cephfs_name()
        sc_data["provisioner"] = (
            provisioner if provisioner else defaults.CEPHFS_PROVISIONER
        )
    sc_data["parameters"]["pool"] = interface_name

    sc_data["metadata"]["name"] = (
        sc_name
        if sc_name
        else create_unique_resource_name(f"test-{interface}", "storageclass")
    )
    sc_data["metadata"]["namespace"] = defaults.ROOK_CLUSTER_NAMESPACE
    sc_data["parameters"]["csi.storage.k8s.io/provisioner-secret-name"] = secret_name
    sc_data["parameters"][
        "csi.storage.k8s.io/provisioner-secret-namespace"
    ] = defaults.ROOK_CLUSTER_NAMESPACE
    sc_data["parameters"][
        "csi.storage.k8s.io/controller-expand-secret-name"
    ] = secret_name
    sc_data["parameters"][
        "csi.storage.k8s.io/controller-expand-secret-namespace"
    ] = defaults.ROOK_CLUSTER_NAMESPACE

    sc_data["parameters"]["clusterID"] = defaults.ROOK_CLUSTER_NAMESPACE
    sc_data["reclaimPolicy"] = reclaim_policy

    try:
        del sc_data["parameters"]["userid"]
    except KeyError:
        pass
    return create_resource(**sc_data)


def create_pvc(
    sc_name,
    pvc_name=None,
    namespace=defaults.ROOK_CLUSTER_NAMESPACE,
    size=None,
    do_reload=True,
    access_mode=constants.ACCESS_MODE_RWO,
    volume_mode=None,
):
    """
    Create a PVC

    Args:
        sc_name (str): The name of the storage class for the PVC to be
            associated with
        pvc_name (str): The name of the PVC to create
        namespace (str): The namespace for the PVC creation
        size (str): Size of pvc to create
        do_reload (bool): True for wait for reloading PVC after its creation, False otherwise
        access_mode (str): The access mode to be used for the PVC
        volume_mode (str): Volume mode for rbd RWX pvc i.e. 'Block'

    Returns:
        PVC: PVC instance
    """
    pvc_data = templating.load_yaml(constants.CSI_PVC_YAML)
    pvc_data["metadata"]["name"] = (
        pvc_name if pvc_name else create_unique_resource_name("test", "pvc")
    )
    pvc_data["metadata"]["namespace"] = namespace
    pvc_data["spec"]["accessModes"] = [access_mode]
    pvc_data["spec"]["storageClassName"] = sc_name
    if size:
        pvc_data["spec"]["resources"]["requests"]["storage"] = size
    if volume_mode:
        pvc_data["spec"]["volumeMode"] = volume_mode
    ocs_obj = pvc.PVC(**pvc_data)
    created_pvc = ocs_obj.create(do_reload=do_reload)
    assert created_pvc, f"Failed to create resource {pvc_name}"
    return ocs_obj


def create_multiple_pvcs(
    sc_name,
    namespace,
    number_of_pvc=1,
    size=None,
    do_reload=False,
    access_mode=constants.ACCESS_MODE_RWO,
    burst=False,
):
    """
    Create one or more PVC as a bulk or one by one

    Args:
        sc_name (str): The name of the storage class to provision the PVCs from
        namespace (str): The namespace for the PVCs creation
        number_of_pvc (int): Number of PVCs to be created
        size (str): The size of the PVCs to create
        do_reload (bool): True for wait for reloading PVC after its creation,
            False otherwise
        access_mode (str): The kind of access mode for PVC

    Returns:
         list: List of PVC objects
    """
    if not burst:
        if access_mode == "ReadWriteMany" and "rbd" in sc_name:
            volume_mode = "Block"
        else:
            volume_mode = None
        return [
            create_pvc(
                sc_name=sc_name,
                size=size,
                namespace=namespace,
                do_reload=do_reload,
                access_mode=access_mode,
                volume_mode=volume_mode,
            )
            for _ in range(number_of_pvc)
        ]

    pvc_data = templating.load_yaml(constants.CSI_PVC_YAML)
    pvc_data["metadata"]["namespace"] = namespace
    pvc_data["spec"]["accessModes"] = [access_mode]
    pvc_data["spec"]["storageClassName"] = sc_name
    if size:
        pvc_data["spec"]["resources"]["requests"]["storage"] = size
    if access_mode == "ReadWriteMany" and "rbd" in sc_name:
        pvc_data["spec"]["volumeMode"] = "Block"
    else:
        pvc_data["spec"]["volumeMode"] = None

    # Creating tem directory to hold the files for the PVC creation
    tmpdir = tempfile.mkdtemp()
    logger.info("Creating the PVC yaml files for creation in bulk")
    ocs_objs = []
    for _ in range(number_of_pvc):
        name = create_unique_resource_name("test", "pvc")
        logger.info(f"Adding PVC with name {name}")
        pvc_data["metadata"]["name"] = name
        templating.dump_data_to_temp_yaml(pvc_data, f"{tmpdir}/{name}.yaml")
        ocs_objs.append(pvc.PVC(**pvc_data))

    logger.info("Creating all PVCs as bulk")
    oc = OCP(kind="pod", namespace=defaults.ROOK_CLUSTER_NAMESPACE)
    cmd = f"create -f {tmpdir}/"
    oc.exec_oc_cmd(command=cmd, out_yaml_format=False)

    # Letting the system 1 sec for each PVC to create.
    # this will prevent any other command from running in the system in this
    # period of time.
    logger.info(
        f"Going to sleep for {number_of_pvc} sec. "
        "until starting verify that PVCs was created."
    )
    time.sleep(number_of_pvc)

    return ocs_objs


def verify_block_pool_exists(pool_name):
    """
    Verify if a Ceph block pool exist

    Args:
        pool_name (str): The name of the Ceph block pool

    Returns:
        bool: True if the Ceph block pool exists, False otherwise
    """
    logger.info(f"Verifying that block pool {pool_name} exists")
    ct_pod = pod.get_ceph_tools_pod()
    try:
        for pools in TimeoutSampler(60, 3, ct_pod.exec_ceph_cmd, "ceph osd lspools"):
            logger.info(f"POOLS are {pools}")
            for pool in pools:
                if pool_name in pool.get("poolname"):
                    return True
    except TimeoutExpiredError:
        return False


def get_pool_cr(pool_name):
    """
    Get the pool CR even if the kind is unknown.

    Args:
         pool_name (str): The name of the pool to get the CR for.

    Returns:
        dict: If the resource is found, None otherwise.

    """
    logger.info(f"Checking if pool {pool_name} is kind of {constants.CEPHBLOCKPOOL}")
    ocp_kind_cephblockpool = ocp.OCP(
        kind=constants.CEPHBLOCKPOOL, namespace=config.ENV_DATA["cluster_namespace"]
    )
    pool_cr = ocp_kind_cephblockpool.get(resource_name=pool_name, dont_raise=True)
    if pool_cr is not None:
        return pool_cr
    else:
        logger.info(
            f"Pool {pool_name} is not kind={constants.CEPHBLOCKPOOL}"
            f", checkging if it is kind={constants.CEPHFILESYSTEM}"
        )
        ocp_kind_cephfilesystem = ocp.OCP(
            kind="CephFilesystem",
            namespace=config.ENV_DATA["cluster_namespace"],
        )
        pool_cr = ocp_kind_cephfilesystem.get(resource_name=pool_name, dont_raise=True)
        return pool_cr


def get_admin_key():
    """
    Fetches admin key secret from Ceph

    Returns:
        str: The admin key
    """
    ct_pod = pod.get_ceph_tools_pod()
    out = ct_pod.exec_ceph_cmd("ceph auth get-key client.admin")
    return out["key"]


def get_cephfs_data_pool_name():
    """
    Fetches ceph fs datapool name from Ceph

    Returns:
        str: fs datapool name
    """
    ct_pod = pod.get_ceph_tools_pod()
    out = ct_pod.exec_ceph_cmd("ceph fs ls")
    return out[0]["data_pools"][0]


def validate_cephfilesystem(fs_name):
    """
    Verify CephFileSystem exists at Ceph and OCP

    Args:
       fs_name (str): The name of the Ceph FileSystem

    Returns:
        bool: True if CephFileSystem is created at Ceph and OCP side else
           will return False with valid msg i.e Failure cause
    """
    cfs = ocp.OCP(
        kind=constants.CEPHFILESYSTEM, namespace=defaults.ROOK_CLUSTER_NAMESPACE
    )
    ct_pod = pod.get_ceph_tools_pod()
    ceph_validate = False
    ocp_validate = False

    result = cfs.get(resource_name=fs_name)
    if result.get("metadata").get("name"):
        logger.info("Filesystem %s got created from Openshift Side", fs_name)
        ocp_validate = True
    else:
        logger.info("Filesystem %s was not create at Openshift Side", fs_name)
        return False

    try:
        for pools in TimeoutSampler(60, 3, ct_pod.exec_ceph_cmd, "ceph fs ls"):
            for out in pools:
                result = out.get("name")
                if result == fs_name:
                    logger.info("FileSystem %s got created from Ceph Side", fs_name)
                    ceph_validate = True
                    break
                else:
                    logger.error("FileSystem %s was not present at Ceph Side", fs_name)
                    ceph_validate = False
            if ceph_validate:
                break
    except TimeoutExpiredError:
        pass

    return True if (ceph_validate and ocp_validate) else False


def get_all_storageclass_names():
    """
    Function for getting all storageclass

    Returns:
         list: list of storageclass name
    """
    sc_obj = ocp.OCP(
        kind=constants.STORAGECLASS, namespace=defaults.ROOK_CLUSTER_NAMESPACE
    )
    result = sc_obj.get()
    sample = result["items"]

    storageclass = [
        item.get("metadata").get("name")
        for item in sample
        if (
            (item.get("metadata").get("name") not in constants.IGNORE_SC_GP2)
            and (item.get("metadata").get("name") not in constants.IGNORE_SC_FLEX)
        )
    ]
    return storageclass


def delete_storageclasses(sc_objs):
    """ "
    Function for Deleting storageclasses

    Args:
        sc_objs (list): List of SC objects for deletion

    Returns:
        bool: True if deletion is successful
    """

    for sc in sc_objs:
        logger.info("Deleting StorageClass with name %s", sc.name)
        sc.delete()
    return True


def get_cephblockpool_names():
    """
    Function for getting all CephBlockPool

    Returns:
         list: list of cephblockpool name
    """
    pool_obj = ocp.OCP(
        kind=constants.CEPHBLOCKPOOL, namespace=defaults.ROOK_CLUSTER_NAMESPACE
    )
    result = pool_obj.get()
    sample = result["items"]
    pool_list = [item.get("metadata").get("name") for item in sample]
    return pool_list


def delete_cephblockpools(cbp_objs):
    """
    Function for deleting CephBlockPool

    Args:
        cbp_objs (list): List of CBP objects for deletion

    Returns:
        bool: True if deletion of CephBlockPool is successful
    """
    for cbp in cbp_objs:
        logger.info("Deleting CephBlockPool with name %s", cbp.name)
        cbp.delete()
    return True


def get_cephfs_name():
    """
    Function to retrive CephFS name
    Returns:
        str: Name of CFS
    """
    ct_pod = pod.get_ceph_tools_pod()
    result = ct_pod.exec_ceph_cmd("ceph fs ls")
    return result[0]["name"]


def pull_images(image_name):
    """
    Function to pull images on all nodes

    Args:
        image_name (str): Name of the container image to be pulled

    Returns: None

    """

    node_objs = node.get_node_objs(node.get_worker_nodes())
    for node_obj in node_objs:
        logging.info(f'pulling image "{image_name}  " on node {node_obj.name}')
        assert node_obj.ocp.exec_oc_debug_cmd(
            node_obj.name, cmd_list=[f"podman pull {image_name}"]
        )


def run_io_with_rados_bench(**kw):
    """
    A task for radosbench. Runs radosbench command on specified pod . If
    parameters are not provided task assumes few default parameters.This task
    runs command in synchronous fashion.

    Args:
        kw (dict): a dictionary of various radosbench parameters.
           ex::

               pool_name:pool
               pg_num:number of pgs for pool
               op: type of operation {read, write}
               cleanup: True OR False

    Returns:
        ret: return value of radosbench command
    """

    logger.info("Running radosbench task")
    ceph_pods = kw.get("ceph_pods")  # list of pod objects of ceph cluster
    config = kw.get("config")

    role = config.get("role", "client")
    clients = [cpod for cpod in ceph_pods if role in cpod.roles]

    idx = config.get("idx", 0)
    client = clients[idx]
    op = config.get("op", "write")
    cleanup = ["--no-cleanup", "--cleanup"][config.get("cleanup", True)]
    pool = config.get("pool")

    block = str(config.get("size", 4 << 20))
    time = config.get("time", 120)
    time = str(time)

    rados_bench = (
        f"rados --no-log-to-stderr "
        f"-b {block} "
        f"-p {pool} "
        f"bench "
        f"{time} "
        f"{op} "
        f"{cleanup} "
    )
    try:
        ret = client.exec_ceph_cmd(ceph_cmd=rados_bench)
    except CommandFailed as ex:
        logger.error(f"Rados bench failed\n Error is: {ex}")
        return False

    logger.info(ret)
    logger.info("Finished radosbench")
    return ret


def get_all_pvs():
    """
    Gets all pv in openshift-storage namespace

    Returns:
         dict: Dict of all pv in openshift-storage namespace
    """
    ocp_pv_obj = ocp.OCP(kind=constants.PV, namespace=defaults.ROOK_CLUSTER_NAMESPACE)
    return ocp_pv_obj.get()


# TODO: revert counts of tries and delay,BZ 1726266


@retry(AssertionError, tries=20, delay=10, backoff=1)
def validate_pv_delete(pv_name):
    """
    validates if pv is deleted after pvc deletion

    Args:
        pv_name (str): pv from pvc to validates
    Returns:
        bool: True if deletion is successful

    Raises:
        AssertionError: If pv is not deleted
    """
    ocp_pv_obj = ocp.OCP(kind=constants.PV, namespace=defaults.ROOK_CLUSTER_NAMESPACE)

    try:
        if ocp_pv_obj.get(resource_name=pv_name):
            msg = f"{constants.PV} {pv_name} is not deleted after PVC deletion"
            raise AssertionError(msg)

    except CommandFailed:
        return True


def create_pods(
    pvc_objs, pod_factory, interface, pods_for_rwx=1, status="", nodes=None
):
    """
    Create pods

    Args:
        pvc_objs (list): List of ocs_ci.ocs.resources.pvc.PVC instances
        pod_factory (function): pod_factory function
        interface (int): Interface type
        pods_for_rwx (int): Number of pods to be created if access mode of
            PVC is RWX
        status (str): If provided, wait for desired state of each pod before
            creating next one
        nodes (list): Node name for each pod will be selected from this list.

    Returns:
        list: list of Pod objects
    """
    pod_objs = []
    nodes_iter = cycle(nodes) if nodes else None

    for pvc_obj in pvc_objs:
        volume_mode = getattr(
            pvc_obj, "volume_mode", pvc_obj.get()["spec"]["volumeMode"]
        )
        access_mode = getattr(pvc_obj, "access_mode", pvc_obj.get_pvc_access_mode)
        if volume_mode == "Block":
            pod_dict = constants.CSI_RBD_RAW_BLOCK_POD_YAML
            raw_block_pv = True
        else:
            raw_block_pv = False
            pod_dict = ""
        if access_mode == constants.ACCESS_MODE_RWX:
            pod_obj_rwx = [
                pod_factory(
                    interface=interface,
                    pvc=pvc_obj,
                    status=status,
                    node_name=next(nodes_iter) if nodes_iter else None,
                    pod_dict_path=pod_dict,
                    raw_block_pv=raw_block_pv,
                )
                for _ in range(1, pods_for_rwx)
            ]
            pod_objs.extend(pod_obj_rwx)
        pod_obj = pod_factory(
            interface=interface,
            pvc=pvc_obj,
            status=status,
            node_name=next(nodes_iter) if nodes_iter else None,
            pod_dict_path=pod_dict,
            raw_block_pv=raw_block_pv,
        )
        pod_objs.append(pod_obj)

    return pod_objs


def create_build_from_docker_image(
    image_name,
    install_package,
    namespace,
    source_image="quay.io/ocsci/fedora",
    source_image_label="latest",
):
    """
    Allows to create a build config using a Dockerfile specified as an
    argument, eg.::

        $ oc new-build -D $'FROM centos:7\\nRUN yum install -y httpd'

    creates a build with ``httpd`` installed.

    Args:
        image_name (str): Name of the image to be created
        source_image (str): Source image to build docker image from,
           defaults to Centos as base image
        namespace (str): project where build config should be created
        source_image_label (str): Tag to use along with the image name,
           defaults to 'latest'
        install_package (str): package to install over the base image

    Returns:
        ocs_ci.ocs.ocp.OCP (obj): The OCP object for the image
        Fails on UnavailableBuildException exception if build creation
        fails

    """
    base_image = source_image + ":" + source_image_label

    if config.DEPLOYMENT.get("disconnected"):
        base_image = mirror_image(image=base_image)

    cmd = f"yum install -y {install_package}"
    http_proxy, https_proxy, no_proxy = get_cluster_proxies()
    if http_proxy:
        cmd = (
            f"http_proxy={http_proxy} https_proxy={https_proxy} "
            f"no_proxy='{no_proxy}' {cmd}"
        )

    docker_file = f"FROM {base_image}\n " f" RUN {cmd}\n" f"CMD tail -f /dev/null"

    command = f"new-build -D $'{docker_file}' --name={image_name}"
    kubeconfig = os.getenv("KUBECONFIG")

    oc_cmd = f"oc -n {namespace} "

    if kubeconfig:
        oc_cmd += f"--kubeconfig {kubeconfig} "
    oc_cmd += command
    logger.info(f"Running command {oc_cmd}")
    result = run(oc_cmd, stdout=PIPE, stderr=PIPE, timeout=15, shell=True)
    if result.stderr.decode():
        raise UnavailableBuildException(
            f"Build creation failed with error: {result.stderr.decode()}"
        )
    out = result.stdout.decode()
    logger.info(out)
    if "Success" in out:
        # Build becomes ready once build pod goes into Completed state
        pod_obj = OCP(kind="Pod", resource_name=image_name)
        if pod_obj.wait_for_resource(
            condition="Completed",
            resource_name=f"{image_name}" + "-1-build",
            timeout=300,
            sleep=30,
        ):
            logger.info(f"build {image_name} ready")
            set_image_lookup(image_name)
            logger.info(f"image {image_name} can now be consumed")
            image_stream_obj = OCP(kind="ImageStream", resource_name=image_name)
            return image_stream_obj
    else:
        raise UnavailableBuildException("Build creation failed")


def set_image_lookup(image_name):
    """
    Function to enable lookup, which allows reference to the image stream tag
    in the image field of the object. Example::

        $ oc set image-lookup mysql
        $ oc run mysql --image=mysql

    Args:
        image_name (str): Name of the image stream to pull
           the image locally

    Returns:
        str: output of set image-lookup command

    """
    ocp_obj = ocp.OCP(kind="ImageStream")
    command = f"set image-lookup {image_name}"
    logger.info(f'image lookup for image"{image_name}" is set')
    status = ocp_obj.exec_oc_cmd(command)
    return status


def get_snapshot_time(interface, snap_name, status):
    """
    Get the starting/ending creation time of a PVC based on provisioner logs

    The time and date extraction code below has been modified to read
    the month and day data in the logs.  This fixes an error where negative
    time values are calculated when test runs cross midnight.  Also, previous
    calculations would not set the year, and so the calculations were done
    as if the year were 1900.  This is not a problem except that 1900 was
    not a leap year and so the next February 29th would throw ValueErrors
    for the whole day.  To avoid this problem, changes were made to also
    include the current year.

    Incorrect times will still be given for tests that cross over from
    December 31 to January 1.

    Args:
        interface (str): The interface backed the PVC
        pvc_name (str / list): Name of the PVC(s) for creation time
                               the list will be list of pvc objects
        status (str): the status that we want to get - Start / End

    Returns:
        datetime object: Time of PVC(s) creation

    """

    def get_pattern_time(log, snapname, pattern):
        """
        Get the time of pattern in the log

        Args:
            log (list): list of all lines in the log file
            snapname (str): the name of the snapshot
            pattern (str): the pattern that need to be found in the log (start / bound)

        Returns:
            str: string of the pattern timestamp in the log, if not found None

        """
        this_year = str(datetime.datetime.now().year)
        for line in log:
            if re.search(snapname, line) and re.search(pattern, line):
                mon_day = " ".join(line.split(" ")[0:2])
                return f"{this_year} {mon_day}"
        return None

    logs = ""

    # the starting and ending time are taken from different logs,
    # the start creation time is taken from the snapshot controller, while
    # the end creation time is taken from the csi snapshot driver
    if status.lower() == "start":
        pattern = "Creating content for snapshot"
        # Get the snapshoter-controller pod
        pod_name = pod.get_csi_snapshoter_pod()
        logs = pod.get_pod_logs(
            pod_name, namespace="openshift-cluster-storage-operator"
        )
    elif status.lower() == "end":
        pattern = "readyToUse true"
        pod_name = pod.get_csi_provisioner_pod(interface)
        # get the logs from the csi-provisioner containers
        for log_pod in pod_name:
            logs += pod.get_pod_logs(log_pod, "csi-snapshotter")
    else:
        logger.error(f"the status {status} is invalid.")
        return None

    logs = logs.split("\n")

    stat = None
    # Extract the time for the one PVC snapshot provisioning
    if isinstance(snap_name, str):
        stat = get_pattern_time(logs, snap_name, pattern)
    # Extract the time for the list of PVCs snapshot provisioning
    if isinstance(snap_name, list):
        all_stats = []
        for snapname in snap_name:
            all_stats.append(get_pattern_time(logs, snapname.name, pattern))
        all_stats = sorted(all_stats)
        if status.lower() == "end":
            stat = all_stats[-1]  # return the highest time
        elif status.lower() == "start":
            stat = all_stats[0]  # return the lowest time
    if stat:
        return datetime.datetime.strptime(stat, DATE_TIME_FORMAT)
    else:
        return None


def measure_snapshot_creation_time(interface, snap_name, snap_con_name, snap_uid=None):
    """
    Measure Snapshot creation time based on logs

    Args:
        snap_name (str): Name of the snapshot for creation time measurement

    Returns:
        float: Creation time for the snapshot

    """
    start = get_snapshot_time(interface, snap_name, status="start")
    end = get_snapshot_time(interface, snap_con_name, status="end")
    logs = ""
    if start and end:
        total = end - start
        return total.total_seconds()
    else:
        # at 4.8 the log messages was changed, so need different parsing
        pod_name = pod.get_csi_provisioner_pod(interface)
        # get the logs from the csi-provisioner containers
        for log_pod in pod_name:
            logger.info(f"Read logs from {log_pod}")
            logs += pod.get_pod_logs(log_pod, "csi-snapshotter")
        logs = logs.split("\n")
        pattern = "CSI CreateSnapshot: snapshot-"
        for line in logs:
            if (
                re.search(snap_uid, line)
                and re.search(pattern, line)
                and re.search("readyToUse \\[true\\]", line)
            ):
                # The creation time log is in nanosecond, so, it need to convert to seconds.
                results = int(line.split()[-5].split(":")[1].replace("]", "")) * (
                    10 ** -9
                )
                return float(f"{results:.3f}")

        return None


def get_provision_time(interface, pvc_name, status="start"):
    """
    Get the starting/ending creation time of a PVC based on provisioner logs

    Args:
        interface (str): The interface backed the PVC
        pvc_name (str / list): Name of the PVC(s) for creation time
                               the list will be list of pvc objects
        status (str): the status that we want to get - Start / End

    Returns:
        datetime object: Time of PVC(s) creation

    """
    # Define the status that need to retrieve
    operation = "started"
    if status.lower() == "end":
        operation = "succeeded"

    this_year = str(datetime.datetime.now().year)
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")

    logs = logs.split("\n")
    # Extract the time for the one PVC provisioning
    if isinstance(pvc_name, str):
        stat = [i for i in logs if re.search(f"provision.*{pvc_name}.*{operation}", i)]
        mon_day = " ".join(stat[0].split(" ")[0:2])
        stat = f"{this_year} {mon_day}"
    # Extract the time for the list of PVCs provisioning
    if isinstance(pvc_name, list):
        all_stats = []
        for pv_name in pvc_name:
            name = pv_name.name
            stat = [i for i in logs if re.search(f"provision.*{name}.*{operation}", i)]
            mon_day = " ".join(stat[0].split(" ")[0:2])
            stat = f"{this_year} {mon_day}"
            all_stats.append(stat)
        all_stats = sorted(all_stats)
        if status.lower() == "end":
            stat = all_stats[-1]  # return the highest time
        elif status.lower() == "start":
            stat = all_stats[0]  # return the lowest time
    return datetime.datetime.strptime(stat, DATE_TIME_FORMAT)


def get_start_creation_time(interface, pvc_name):
    """
    Get the starting creation time of a PVC based on provisioner logs

    Args:
        interface (str): The interface backed the PVC
        pvc_name (str): Name of the PVC for creation time measurement

    Returns:
        datetime object: Start time of PVC creation

    """
    this_year = str(datetime.datetime.now().year)
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")

    logs = logs.split("\n")
    # Extract the starting time for the PVC provisioning
    start = [i for i in logs if re.search(f"provision.*{pvc_name}.*started", i)]
    mon_day = " ".join(start[0].split(" ")[0:2])
    start = f"{this_year} {mon_day}"
    return datetime.datetime.strptime(start, DATE_TIME_FORMAT)


def get_end_creation_time(interface, pvc_name):
    """
    Get the ending creation time of a PVC based on provisioner logs

    Args:
        interface (str): The interface backed the PVC
        pvc_name (str): Name of the PVC for creation time measurement

    Returns:
        datetime object: End time of PVC creation

    """
    this_year = str(datetime.datetime.now().year)
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")

    logs = logs.split("\n")
    # Extract the starting time for the PVC provisioning
    end = [i for i in logs if re.search(f"provision.*{pvc_name}.*succeeded", i)]
    # End provisioning string may appear in logs several times, take here the latest one
    mon_day = " ".join(end[-1].split(" ")[0:2])
    end = f"{this_year} {mon_day}"
    return datetime.datetime.strptime(end, DATE_TIME_FORMAT)


def measure_pvc_creation_time(interface, pvc_name):
    """
    Measure PVC creation time based on logs

    Args:
        interface (str): The interface backed the PVC pvc_name (str): Name of the PVC for creation time measurement
    Returns:
        float: Creation time for the PVC

    """
    start = get_start_creation_time(interface=interface, pvc_name=pvc_name)
    end = get_end_creation_time(interface=interface, pvc_name=pvc_name)
    total = end - start
    return total.total_seconds()


def measure_pvc_creation_time_bulk(interface, pvc_name_list, wait_time=60):
    """
    Measure PVC creation time of bulk PVC based on logs.

    Args:
        interface (str): The interface backed the PVC
        pvc_name_list (list): List of PVC Names for measuring creation time
        wait_time (int): Seconds to wait before collecting CSI log

    Returns:
        pvc_dict (dict): Dictionary of pvc_name with creation time.

    """
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # due to some delay in CSI log generation added wait
    time.sleep(wait_time)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")
    logs = logs.split("\n")

    loop_counter = 0
    while True:
        no_data_list = list()
        for name in pvc_name_list:
            # check if PV data present in CSI logs
            start = [i for i in logs if re.search(f"provision.*{name}.*started", i)]
            end = [i for i in logs if re.search(f"provision.*{name}.*succeeded", i)]
            if not start or not end:
                no_data_list.append(name)

        if no_data_list:
            # Clear and get CSI logs after 60secs
            logging.info(f"PVC count without CSI create log data {len(no_data_list)}")
            logs.clear()
            time.sleep(wait_time)
            logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
            logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")
            logs = logs.split("\n")
            loop_counter += 1
            if loop_counter >= 6:
                logging.info("Waited for more than 6mins still no data")
                raise UnexpectedBehaviour(
                    f"There is no pvc creation data in CSI logs for {no_data_list}"
                )
            continue
        else:
            break

    pvc_dict = dict()
    this_year = str(datetime.datetime.now().year)
    for pvc_name in pvc_name_list:
        # Extract the starting time for the PVC provisioning
        start = [i for i in logs if re.search(f"provision.*{pvc_name}.*started", i)]
        mon_day = " ".join(start[0].split(" ")[0:2])
        start = f"{this_year} {mon_day}"
        start_time = datetime.datetime.strptime(start, DATE_TIME_FORMAT)
        # Extract the end time for the PVC provisioning
        end = [i for i in logs if re.search(f"provision.*{pvc_name}.*succeeded", i)]
        mon_day = " ".join(end[0].split(" ")[0:2])
        end = f"{this_year} {mon_day}"
        end_time = datetime.datetime.strptime(end, DATE_TIME_FORMAT)
        total = end_time - start_time
        pvc_dict[pvc_name] = total.total_seconds()

    return pvc_dict


def measure_pv_deletion_time_bulk(interface, pv_name_list, wait_time=60):
    """
    Measure PV deletion time of bulk PV, based on logs.

    Args:
        interface (str): The interface backed the PV
        pv_name_list (list): List of PV Names for measuring deletion time
        wait_time (int): Seconds to wait before collecting CSI log

    Returns:
        pv_dict (dict): Dictionary of pv_name with deletion time.

    """
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # due to some delay in CSI log generation added wait
    time.sleep(wait_time)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")
    logs = logs.split("\n")

    loop_counter = 0
    while True:
        no_data_list = list()
        for pv in pv_name_list:
            # check if PV data present in CSI logs
            start = [i for i in logs if re.search(f'delete "{pv}": started', i)]
            end = [i for i in logs if re.search(f'delete "{pv}": succeeded', i)]
            if not start or not end:
                no_data_list.append(pv)

        if no_data_list:
            # Clear and get CSI logs after 60secs
            logging.info(f"PV count without CSI delete log data {len(no_data_list)}")
            logs.clear()
            time.sleep(wait_time)
            logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
            logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")
            logs = logs.split("\n")
            loop_counter += 1
            if loop_counter >= 6:
                logging.info("Waited for more than 6mins still no data")
                raise UnexpectedBehaviour(
                    f"There is no pv deletion data in CSI logs for {no_data_list}"
                )
            continue
        else:
            break

    pv_dict = dict()
    this_year = str(datetime.datetime.now().year)
    for pv_name in pv_name_list:
        # Extract the deletion start time for the PV
        start = [i for i in logs if re.search(f'delete "{pv_name}": started', i)]
        mon_day = " ".join(start[0].split(" ")[0:2])
        start = f"{this_year} {mon_day}"
        start_time = datetime.datetime.strptime(start, DATE_TIME_FORMAT)
        # Extract the deletion end time for the PV
        end = [i for i in logs if re.search(f'delete "{pv_name}": succeeded', i)]
        mon_day = " ".join(end[0].split(" ")[0:2])
        end_tm = f"{this_year} {mon_day}"
        end_time = datetime.datetime.strptime(end_tm, DATE_TIME_FORMAT)
        total = end_time - start_time
        pv_dict[pv_name] = total.total_seconds()

    return pv_dict


def get_start_deletion_time(interface, pv_name):
    """
    Get the starting deletion time of a PVC based on provisioner logs

    Args:
        interface (str): The interface backed the PVC
        pvc_name (str): Name of the PVC for deletion time measurement

    Returns:
        datetime object: Start time of PVC deletion

    """
    this_year = str(datetime.datetime.now().year)
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")

    logs = logs.split("\n")
    # Extract the starting time for the PVC deletion
    start = [i for i in logs if re.search(f'delete "{pv_name}": started', i)]
    mon_day = " ".join(start[0].split(" ")[0:2])
    start = f"{this_year} {mon_day}"
    return datetime.datetime.strptime(start, DATE_TIME_FORMAT)


def get_end_deletion_time(interface, pv_name):
    """
    Get the ending deletion time of a PVC based on provisioner logs

    Args:
        interface (str): The interface backed the PVC
        pv_name (str): Name of the PVC for deletion time measurement

    Returns:
        datetime object: End time of PVC deletion

    """
    this_year = str(datetime.datetime.now().year)
    # Get the correct provisioner pod based on the interface
    pod_name = pod.get_csi_provisioner_pod(interface)
    # get the logs from the csi-provisioner containers
    logs = pod.get_pod_logs(pod_name[0], "csi-provisioner")
    logs += pod.get_pod_logs(pod_name[1], "csi-provisioner")

    logs = logs.split("\n")
    # Extract the starting time for the PV deletion
    end = [i for i in logs if re.search(f'delete "{pv_name}": succeeded', i)]
    mon_day = " ".join(end[0].split(" ")[0:2])
    end = f"{this_year} {mon_day}"
    return datetime.datetime.strptime(end, DATE_TIME_FORMAT)


def measure_pvc_deletion_time(interface, pv_name):
    """
    Measure PVC deletion time based on logs

    Args:
        interface (str): The interface backed the PVC
        pv_name (str): Name of the PV for creation time measurement

    Returns:
        float: Deletion time for the PVC

    """
    start = get_start_deletion_time(interface=interface, pv_name=pv_name)
    end = get_end_deletion_time(interface=interface, pv_name=pv_name)
    total = end - start
    return total.total_seconds()


def pod_start_time(pod_obj):
    """
    Function to measure time taken for container(s) to get into running state
    by measuring the difference between container's start time (when container
    went into running state) and started time (when container was actually
    started)

    Args:
        pod_obj(obj): pod object to measure start time

    Returns:
        containers_start_time(dict):
        Returns the name and start time of container(s) in a pod

    """
    time_format = "%Y-%m-%dT%H:%M:%SZ"
    containers_start_time = {}
    start_time = pod_obj.data["status"]["startTime"]
    start_time = datetime.datetime.strptime(start_time, time_format)
    for container in range(len(pod_obj.data["status"]["containerStatuses"])):
        started_time = pod_obj.data["status"]["containerStatuses"][container]["state"][
            "running"
        ]["startedAt"]
        started_time = datetime.datetime.strptime(started_time, time_format)
        container_name = pod_obj.data["status"]["containerStatuses"][container]["name"]
        container_start_time = (started_time - start_time).seconds
        containers_start_time[container_name] = container_start_time
        return containers_start_time


def get_default_storage_class():
    """
    Get the default StorageClass(es)

    Returns:
        list: default StorageClass(es) list

    """
    default_sc_obj = ocp.OCP(kind="StorageClass")
    storage_classes = default_sc_obj.get().get("items")
    storage_classes = [
        sc for sc in storage_classes if "annotations" in sc.get("metadata")
    ]
    return [
        sc.get("metadata").get("name")
        for sc in storage_classes
        if sc.get("metadata")
               .get("annotations")
               .get("storageclass.kubernetes.io/is-default-class")
           == "true"
    ]


def change_default_storageclass(scname):
    """
    Change the default StorageClass to the given SC name

    Args:
        scname (str): StorageClass name

    Returns:
        bool: True on success

    """
    default_sc = get_default_storage_class()
    ocp_obj = ocp.OCP(kind="StorageClass")
    if default_sc:
        # Change the existing default Storageclass annotation to false
        patch = (
            ' \'{"metadata": {"annotations":'
            '{"storageclass.kubernetes.io/is-default-class"'
            ':"false"}}}\' '
        )
        patch_cmd = f"patch storageclass {default_sc} -p" + patch
        ocp_obj.exec_oc_cmd(command=patch_cmd)

    # Change the new storageclass to default
    patch = (
        ' \'{"metadata": {"annotations":'
        '{"storageclass.kubernetes.io/is-default-class"'
        ':"true"}}}\' '
    )
    patch_cmd = f"patch storageclass {scname} -p" + patch
    ocp_obj.exec_oc_cmd(command=patch_cmd)
    return True


def is_volume_present_in_backend(interface, image_uuid, pool_name=None):
    """
    Check whether Image/Subvolume is present in the backend.

    Args:
        interface (str): The interface backed the PVC
        image_uuid (str): Part of VolID which represents corresponding
          image/subvolume in backend, eg:
          ``oc get pv/<volumeName> -o jsonpath='{.spec.csi.volumeHandle}'``
          Output is the CSI generated VolID and looks like:
          ``0001-000c-rook-cluster-0000000000000001-f301898c-a192-11e9-852a-1eeeb6975c91``
          where image_uuid is ``f301898c-a192-11e9-852a-1eeeb6975c91``
        pool_name (str): Name of the rbd-pool if interface is CephBlockPool

    Returns:
        bool: True if volume is present and False if volume is not present

    """
    cmd = ""
    valid_error = []
    ct_pod = pod.get_ceph_tools_pod()
    if interface == constants.CEPHBLOCKPOOL:
        valid_error = [f"error opening image csi-vol-{image_uuid}"]
        cmd = f"rbd info -p {pool_name} csi-vol-{image_uuid}"
    if interface == constants.CEPHFILESYSTEM:
        valid_error = [
            f"Subvolume 'csi-vol-{image_uuid}' not found",
            f"subvolume 'csi-vol-{image_uuid}' does not exist",
        ]
        cmd = (
            f"ceph fs subvolume getpath {get_cephfs_name()}"
            f" csi-vol-{image_uuid} csi"
        )

    try:
        ct_pod.exec_ceph_cmd(ceph_cmd=cmd, format="json")
        logger.info(
            f"Verified: Volume corresponding to uuid {image_uuid} exists " f"in backend"
        )
        return True
    except CommandFailed as ecf:
        assert any([error in str(ecf) for error in valid_error]), (
            f"Error occurred while verifying volume is present in backend: "
            f"{str(ecf)} ImageUUID: {image_uuid}. Interface type: {interface}"
        )
        logger.info(
            f"Volume corresponding to uuid {image_uuid} does not exist " f"in backend"
        )
        return False


def verify_volume_deleted_in_backend(
    interface, image_uuid, pool_name=None, timeout=180
):
    """
    Ensure that Image/Subvolume is deleted in the backend.

    Args:
        interface (str): The interface backed the PVC
        image_uuid (str): Part of VolID which represents corresponding
          image/subvolume in backend, eg:
          ``oc get pv/<volumeName> -o jsonpath='{.spec.csi.volumeHandle}'``
          Output is the CSI generated VolID and looks like:
          ``0001-000c-rook-cluster-0000000000000001-f301898c-a192-11e9-852a-1eeeb6975c91``
          where image_uuid is ``f301898c-a192-11e9-852a-1eeeb6975c91``
        pool_name (str): Name of the rbd-pool if interface is CephBlockPool
        timeout (int): Wait time for the volume to be deleted.

    Returns:
        bool: True if volume is deleted before timeout.
            False if volume is not deleted.
    """
    try:
        for ret in TimeoutSampler(
            timeout,
            2,
            is_volume_present_in_backend,
            interface=interface,
            image_uuid=image_uuid,
            pool_name=pool_name,
        ):
            if not ret:
                break
        logger.info(
            f"Verified: Volume corresponding to uuid {image_uuid} is deleted "
            f"in backend"
        )
        return True
    except TimeoutExpiredError:
        logger.error(
            f"Volume corresponding to uuid {image_uuid} is not deleted " f"in backend"
        )
        # Log 'ceph progress' and 'ceph rbd task list' for debugging purpose
        ct_pod = pod.get_ceph_tools_pod()
        ct_pod.exec_ceph_cmd("ceph progress json", format=None)
        ct_pod.exec_ceph_cmd("ceph rbd task list")
        return False


def delete_volume_in_backend(img_uuid, pool_name=None):
    """
    Delete an Image/Subvolume in the backend

    Args:
         img_uuid (str): Part of VolID which represents corresponding
            image/subvolume in backend, eg:
            ``oc get pv/<volumeName> -o jsonpath='{.spec.csi.volumeHandle}'``
            Output is the CSI generated VolID and looks like:
            ``0001-000c-rook-cluster-0000000000000001-f301898c-a192-11e9-852a-1eeeb6975c91``
            where image_uuid is ``f301898c-a192-11e9-852a-1eeeb6975c91``
         pool_name (str): The of the pool

    Returns:
         bool: True if image deleted successfully
            False if:
                Pool not found
                image not found
                image not deleted

    """
    cmd = ""
    valid_error = []
    pool_cr = get_pool_cr(pool_name)
    if pool_cr is not None:
        if pool_cr["kind"] == "CephFilesystem":
            interface = "CephFileSystem"
        else:
            interface = pool_cr["kind"]
        logger.info(f"pool {pool_cr} kind is {interface}")
    else:
        logger.info(
            f"Pool {pool_name} has no kind of "
            f"{constants.CEPHBLOCKPOOL} "
            f"or {constants.CEPHFILESYSTEM}"
        )
        return False

    # Checking if image is present before trying to delete
    image_present_results = is_volume_present_in_backend(
        interface=interface, image_uuid=img_uuid, pool_name=pool_name
    )

    # Incase image is present delete
    if image_present_results:
        if interface == constants.CEPHBLOCKPOOL:
            logger.info(
                f"Trying to delete image csi-vol-{img_uuid} from pool {pool_name}"
            )
            valid_error = ["No such file or directory"]
            cmd = f"rbd rm -p {pool_name} csi-vol-{img_uuid}"

        if interface == constants.CEPHFILESYSTEM:
            logger.info(
                f"Trying to delete image csi-vol-{img_uuid} from pool {pool_name}"
            )
            valid_error = [
                f"Subvolume 'csi-vol-{img_uuid}' not found",
                f"subvolume 'csi-vol-{img_uuid}' does not exist",
            ]
            cmd = f"ceph fs subvolume rm {get_cephfs_name()} csi-vol-{img_uuid} csi"

        ct_pod = pod.get_ceph_tools_pod()
        try:
            ct_pod.exec_ceph_cmd(ceph_cmd=cmd, format=None)
        except CommandFailed as ecf:
            if any([error in str(ecf) for error in valid_error]):
                logger.info(
                    f"Error occurred while verifying volume is present in backend: "
                    f"{str(ecf)} ImageUUID: {img_uuid}. Interface type: {interface}"
                )
                return False

        verify_img_delete_result = is_volume_present_in_backend(
            interface=interface, image_uuid=img_uuid, pool_name=pool_name
        )
        if not verify_img_delete_result:
            logger.info(f"Image csi-vol-{img_uuid} deleted successfully")
            return True
        else:
            logger.info(f"Image csi-vol-{img_uuid} not deleted successfully")
            return False
    return False


def create_serviceaccount(namespace):
    """
    Create a Serviceaccount

    Args:
        namespace (str): The namespace for the serviceaccount creation

    Returns:
        OCS: An OCS instance for the service_account
    """

    service_account_data = templating.load_yaml(constants.SERVICE_ACCOUNT_YAML)
    service_account_data["metadata"]["name"] = create_unique_resource_name(
        "sa", "serviceaccount"
    )
    service_account_data["metadata"]["namespace"] = namespace

    return create_resource(**service_account_data)


def get_serviceaccount_obj(sa_name, namespace):
    """
    Get serviceaccount obj

    Args:
        sa_name (str): Service Account name
        namespace (str): The namespace for the serviceaccount creation

    Returns:
        OCS: An OCS instance for the service_account
    """
    ocp_sa_obj = ocp.OCP(kind=constants.SERVICE_ACCOUNT, namespace=namespace)
    try:
        sa_dict = ocp_sa_obj.get(resource_name=sa_name)
        return OCS(**sa_dict)

    except CommandFailed:
        logger.error("ServiceAccount not found in specified namespace")


def validate_scc_policy(sa_name, namespace, scc_name=constants.PRIVILEGED):
    """
    Validate serviceaccount is added to scc of privileged

    Args:
        sa_name (str): Service Account name
        namespace (str): The namespace for the serviceaccount creation
        scc_name (str): SCC name

    Returns:
        bool: True if sc_name is present in scc of privileged else False
    """
    sa_name = f"system:serviceaccount:{namespace}:{sa_name}"
    logger.info(sa_name)
    ocp_scc_obj = ocp.OCP(kind=constants.SCC, namespace=namespace)
    scc_dict = ocp_scc_obj.get(resource_name=scc_name)
    scc_users_list = scc_dict.get("users")
    for scc_user in scc_users_list:
        if scc_user == sa_name:
            return True
    return False


def add_scc_policy(sa_name, namespace):
    """
    Adding ServiceAccount to scc privileged

    Args:
        sa_name (str): ServiceAccount name
        namespace (str): The namespace for the scc_policy creation

    """
    ocp = OCP()
    out = ocp.exec_oc_cmd(
        command=f"adm policy add-scc-to-user privileged system:serviceaccount:{namespace}:{sa_name}",
        out_yaml_format=False,
    )

    logger.info(out)


def remove_scc_policy(sa_name, namespace):
    """
    Removing ServiceAccount from scc privileged

    Args:
        sa_name (str): ServiceAccount name
        namespace (str): The namespace for the scc_policy deletion

    """
    ocp = OCP()
    out = ocp.exec_oc_cmd(
        command=f"adm policy remove-scc-from-user privileged system:serviceaccount:{namespace}:{sa_name}",
        out_yaml_format=False,
    )

    logger.info(out)


def craft_s3_command(cmd, mcg_obj=None, api=False):
    """
    Crafts the AWS CLI S3 command including the
    login credentials and command to be ran

    Args:
        mcg_obj: An MCG object containing the MCG S3 connection credentials
        cmd: The AWSCLI command to run
        api: True if the call is for s3api, false if s3

    Returns:
        str: The crafted command, ready to be executed on the pod

    """
    api = "api" if api else ""
    if mcg_obj:
        base_command = (
            f'sh -c "AWS_CA_BUNDLE={constants.SERVICE_CA_CRT_AWSCLI_PATH} '
            f"AWS_ACCESS_KEY_ID={mcg_obj.access_key_id} "
            f"AWS_SECRET_ACCESS_KEY={mcg_obj.access_key} "
            f"AWS_DEFAULT_REGION={mcg_obj.region} "
            f"aws s3{api} "
            f"--endpoint={mcg_obj.s3_internal_endpoint} "
        )
        string_wrapper = '"'
    else:
        base_command = f"aws s3{api} --no-sign-request "
        string_wrapper = ""
    return f"{base_command}{cmd}{string_wrapper}"


def wait_for_resource_count_change(
    func_to_use,
    previous_num,
    namespace,
    change_type="increase",
    min_difference=1,
    timeout=20,
    interval=2,
    **func_kwargs,
):
    """
    Wait for a change in total count of PVC or pod

    Args:
        func_to_use (function): Function to be used to fetch resource info
            Supported functions: pod.get_all_pvcs(), pod.get_all_pods()
        previous_num (int): Previous number of pods/PVCs for comparison
        namespace (str): Name of the namespace
        change_type (str): Type of change to check. Accepted values are
            'increase' and 'decrease'. Default is 'increase'.
        min_difference (int): Minimum required difference in PVC/pod count
        timeout (int): Maximum wait time in seconds
        interval (int): Time in seconds to wait between consecutive checks

    Returns:
        True if difference in count is greater than or equal to
            'min_difference'. False in case of timeout.
    """
    try:
        for sample in TimeoutSampler(
            timeout, interval, func_to_use, namespace, **func_kwargs
        ):
            if func_to_use == pod.get_all_pods:
                current_num = len(sample)
            else:
                current_num = len(sample["items"])

            if change_type == "increase":
                count_diff = current_num - previous_num
            else:
                count_diff = previous_num - current_num
            if count_diff >= min_difference:
                return True
    except TimeoutExpiredError:
        return False


def verify_pv_mounted_on_node(node_pv_dict):
    """
    Check if mount point of a PV exists on a node

    Args:
        node_pv_dict (dict): Node to PV list mapping
            eg: {'node1': ['pv1', 'pv2', 'pv3'], 'node2': ['pv4', 'pv5']}

    Returns:
        dict: Node to existing PV list mapping
            eg: {'node1': ['pv1', 'pv3'], 'node2': ['pv5']}
    """
    existing_pvs = {}
    for node_name, pvs in node_pv_dict.items():
        cmd = f"oc debug nodes/{node_name} -- df"
        df_on_node = run_cmd(cmd)
        existing_pvs[node_name] = []
        for pv_name in pvs:
            if f"/pv/{pv_name}/" in df_on_node:
                existing_pvs[node_name].append(pv_name)
    return existing_pvs


def converge_lists(list_to_converge):
    """
    Function to flatten and remove the sublist created during future obj

    Args:
       list_to_converge (list): arg list of lists, eg: [[1,2],[3,4]]

    Returns:
        list (list): return converged list eg: [1,2,3,4]
    """
    return [item for sublist in list_to_converge for item in sublist]


def create_multiple_pvc_parallel(sc_obj, namespace, number_of_pvc, size, access_modes):
    """
    Funtion to create multiple PVC in parallel using threads
    Function will create PVCs based on the available access modes

    Args:
        sc_obj (str): Storage Class object
        namespace (str): The namespace for creating pvc
        number_of_pvc (int): NUmber of pvc to be created
        size (str): size of the pvc eg: '10Gi'
        access_modes (list): List of access modes for PVC creation

    Returns:
        pvc_objs_list (list): List of pvc objs created in function
    """
    obj_status_list, result_lists = ([] for i in range(2))
    with ThreadPoolExecutor() as executor:
        for mode in access_modes:
            result_lists.append(
                executor.submit(
                    create_multiple_pvcs,
                    sc_name=sc_obj.name,
                    namespace=namespace,
                    number_of_pvc=number_of_pvc,
                    access_mode=mode,
                    size=size,
                )
            )
    result_list = [result.result() for result in result_lists]
    pvc_objs_list = converge_lists(result_list)
    # Check for all the pvcs in Bound state
    with ThreadPoolExecutor() as executor:
        for objs in pvc_objs_list:
            obj_status_list.append(
                executor.submit(wait_for_resource_state, objs, "Bound", 90)
            )
    if False in [obj.result() for obj in obj_status_list]:
        raise TimeoutExpiredError
    return pvc_objs_list


def create_pods_parallel(
    pvc_list,
    namespace,
    interface,
    pod_dict_path=None,
    sa_name=None,
    raw_block_pv=False,
    dc_deployment=False,
    node_selector=None,
):
    """
    Function to create pods in parallel

    Args:
        pvc_list (list): List of pvcs to be attached in pods
        namespace (str): The namespace for creating pod
        interface (str): The interface backed the PVC
        pod_dict_path (str): pod_dict_path for yaml
        sa_name (str): sa_name for providing permission
        raw_block_pv (bool): Either RAW block or not
        dc_deployment (bool): Either DC deployment or not
        node_selector (dict): dict of key-value pair to be used for nodeSelector field
            eg: {'nodetype': 'app-pod'}

    Returns:
        pod_objs (list): Returns list of pods created
    """
    future_pod_objs = []
    # Added 300 sec wait time since in scale test once the setup has more
    # PODs time taken for the pod to be up will be based on resource available
    wait_time = 300
    if raw_block_pv and not pod_dict_path:
        pod_dict_path = constants.CSI_RBD_RAW_BLOCK_POD_YAML
    with ThreadPoolExecutor() as executor:
        for pvc_obj in pvc_list:
            future_pod_objs.append(
                executor.submit(
                    create_pod,
                    interface_type=interface,
                    pvc_name=pvc_obj.name,
                    do_reload=False,
                    namespace=namespace,
                    raw_block_pv=raw_block_pv,
                    pod_dict_path=pod_dict_path,
                    sa_name=sa_name,
                    dc_deployment=dc_deployment,
                    node_selector=node_selector,
                )
            )
    pod_objs = [pvc_obj.result() for pvc_obj in future_pod_objs]
    # Check for all the pods are in Running state
    # In above pod creation not waiting for the pod to be created because of threads usage
    with ThreadPoolExecutor() as executor:
        for obj in pod_objs:
            future_pod_objs.append(
                executor.submit(
                    wait_for_resource_state, obj, "Running", timeout=wait_time
                )
            )
    # If pods not up raise exception/failure
    if False in [obj.result() for obj in future_pod_objs]:
        raise TimeoutExpiredError
    return pod_objs


def delete_objs_parallel(obj_list):
    """
    Function to delete objs specified in list
    Args:
        obj_list(list): List can be obj of pod, pvc, etc

    Returns:
        bool: True if obj deleted else False

    """
    threads = list()
    for obj in obj_list:
        process = threading.Thread(target=obj.delete)
        process.start()
        threads.append(process)
    for process in threads:
        process.join()
    return True


def memory_leak_analysis(median_dict):
    """
    Function to analyse Memory leak after execution of test case Memory leak is
    analyzed based on top output "RES" value of ceph-osd daemon, i.e.
    ``list[7]`` in code.

    More Detail on Median value: For calculating memory leak require a constant
    value, which should not be start or end of test, so calculating it by
    getting memory for 180 sec before TC execution and take a median out of it.
    Memory value could be different for each nodes, so identify constant value
    for each node and update in median_dict

    Args:
         median_dict (dict): dict of worker nodes and respective median value
         eg: median_dict = {'worker_node_1':102400, 'worker_node_2':204800, ...}

    Usage::

        test_case(.., memory_leak_function):
            .....
            median_dict = helpers.get_memory_leak_median_value()
            .....
            TC execution part, memory_leak_fun will capture data
            ....
            helpers.memory_leak_analysis(median_dict)
            ....
    """
    # dict to store memory leak difference for each worker
    diff = {}
    for worker in node.get_worker_nodes():
        memory_leak_data = []
        if os.path.exists(f"/tmp/{worker}-top-output.txt"):
            with open(f"/tmp/{worker}-top-output.txt", "r") as f:
                data = f.readline()
                list = data.split(" ")
                list = [i for i in list if i]
                memory_leak_data.append(list[7])
        else:
            logging.info(f"worker {worker} memory leak file not found")
            raise UnexpectedBehaviour
        number_of_lines = len(memory_leak_data) - 1
        # Get the start value form median_dict arg for respective worker
        start_value = median_dict[f"{worker}"]
        end_value = memory_leak_data[number_of_lines]
        logging.info(f"Median value {start_value}")
        logging.info(f"End value {end_value}")
        # Convert the values to kb for calculations
        if start_value.__contains__("g"):
            start_value = float(1024 ** 2 * float(start_value[:-1]))
        elif start_value.__contains__("m"):
            start_value = float(1024 * float(start_value[:-1]))
        else:
            start_value = float(start_value)
        if end_value.__contains__("g"):
            end_value = float(1024 ** 2 * float(end_value[:-1]))
        elif end_value.__contains__("m"):
            end_value = float(1024 * float(end_value[:-1]))
        else:
            end_value = float(end_value)
        # Calculate the percentage of diff between start and end value
        # Based on value decide TC pass or fail
        diff[worker] = ((end_value - start_value) / start_value) * 100
        logging.info(f"Percentage diff in start and end value {diff[worker]}")
        if diff[worker] <= 20:
            logging.info(f"No memory leak in worker {worker} passing the test")
        else:
            logging.info(f"There is a memory leak in worker {worker}")
            logging.info(f"Memory median value start of the test {start_value}")
            logging.info(f"Memory value end of the test {end_value}")
            raise UnexpectedBehaviour


def get_memory_leak_median_value():
    """
    Function to calculate memory leak Median value by collecting the data for 180 sec
    and find the median value which will be considered as starting point
    to evaluate memory leak using "RES" value of ceph-osd daemon i.e. list[7] in code

    Returns:
        median_dict (dict): dict of worker nodes and respective median value
    """
    median_dict = {}
    timeout = 180  # wait for 180 sec to evaluate  memory leak median data.
    logger.info(f"waiting for {timeout} sec to evaluate the median value")
    time.sleep(timeout)
    for worker in node.get_worker_nodes():
        memory_leak_data = []
        if os.path.exists(f"/tmp/{worker}-top-output.txt"):
            with open(f"/tmp/{worker}-top-output.txt", "r") as f:
                data = f.readline()
                list = data.split(" ")
                list = [i for i in list if i]
                memory_leak_data.append(list[7])
        else:
            logging.info(f"worker {worker} memory leak file not found")
            raise UnexpectedBehaviour
        median_dict[f"{worker}"] = statistics.median(memory_leak_data)
    return median_dict


def refresh_oc_login_connection(user=None, password=None):
    """
    Function to refresh oc user login
    Default login using kubeadmin user and password

    Args:
        user (str): Username to login
        password (str): Password to login

    """
    user = user or config.RUN["username"]
    if not password:
        filename = os.path.join(
            config.ENV_DATA["cluster_path"], config.RUN["password_location"]
        )
        with open(filename) as f:
            password = f.read()
    ocs_obj = ocp.OCP()
    ocs_obj.login(user=user, password=password)


def rsync_kubeconf_to_node(node):
    """
    Function to copy kubeconfig to OCP node

    Args:
        node (str): OCP node to copy kubeconfig if not present

    """
    # ocp_obj = ocp.OCP()
    filename = os.path.join(
        config.ENV_DATA["cluster_path"], config.RUN["kubeconfig_location"]
    )
    file_path = os.path.dirname(filename)
    master_list = node.get_master_nodes()
    ocp_obj = ocp.OCP()
    check_auth = "auth"
    check_conf = "kubeconfig"
    node_path = "/home/core/"
    if check_auth not in ocp_obj.exec_oc_debug_cmd(
        node=master_list[0], cmd_list=[f"ls {node_path}"]
    ):
        ocp.rsync(src=file_path, dst=f"{node_path}", node=node, dst_node=True)
    elif check_conf not in ocp_obj.exec_oc_debug_cmd(
        node=master_list[0], cmd_list=[f"ls {node_path}auth"]
    ):
        ocp.rsync(src=file_path, dst=f"{node_path}", node=node, dst_node=True)


def create_dummy_osd(deployment):
    """
    Replace one of OSD pods with pod that contains all data from original
    OSD but doesn't run osd daemon. This can be used e.g. for direct acccess
    to Ceph Placement Groups.

    Args:
        deployment (str): Name of deployment to use

    Returns:
        list: first item is dummy deployment object, second item is dummy pod
            object
    """
    oc = OCP(
        kind=constants.DEPLOYMENT, namespace=config.ENV_DATA.get("cluster_namespace")
    )
    osd_data = oc.get(deployment)
    dummy_deployment = create_unique_resource_name("dummy", "osd")
    osd_data["metadata"]["name"] = dummy_deployment

    osd_containers = osd_data.get("spec").get("template").get("spec").get("containers")
    # get osd container spec
    original_osd_args = osd_containers[0].get("args")
    osd_data["spec"]["template"]["spec"]["containers"][0]["args"] = []
    osd_data["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "/bin/bash",
        "-c",
        "sleep infinity",
    ]
    osd_file = tempfile.NamedTemporaryFile(
        mode="w+", prefix=dummy_deployment, delete=False
    )
    with open(osd_file.name, "w") as temp:
        yaml.dump(osd_data, temp)
    oc.create(osd_file.name)

    # downscale the original deployment and start dummy deployment instead
    oc.exec_oc_cmd(f"scale --replicas=0 deployment/{deployment}")
    oc.exec_oc_cmd(f"scale --replicas=1 deployment/{dummy_deployment}")

    osd_list = pod.get_osd_pods()
    dummy_pod = [pod for pod in osd_list if dummy_deployment in pod.name][0]
    wait_for_resource_state(
        resource=dummy_pod, state=constants.STATUS_RUNNING, timeout=60
    )
    ceph_init_cmd = "/rook/tini" + " " + " ".join(original_osd_args)
    try:
        logger.info("Following command should expire after 7 seconds")
        dummy_pod.exec_cmd_on_pod(ceph_init_cmd, timeout=7)
    except TimeoutExpired:
        logger.info("Killing /rook/tini process")
        try:
            dummy_pod.exec_sh_cmd_on_pod(
                "kill $(ps aux | grep '[/]rook/tini' | awk '{print $2}')"
            )
        except CommandFailed:
            pass

    return dummy_deployment, dummy_pod


def get_failure_domin():
    """
    Function is used to getting failure domain of pool

    Returns:
        str: Failure domain from cephblockpool

    """
    ct_pod = pod.get_ceph_tools_pod()
    out = ct_pod.exec_ceph_cmd(ceph_cmd="ceph osd crush rule dump", format="json")
    assert out, "Failed to get cmd output"
    for crush_rule in out:
        if constants.CEPHBLOCKPOOL.lower() in crush_rule.get("rule_name"):
            for steps in crush_rule.get("steps"):
                if "type" in steps:
                    return steps.get("type")


def wait_for_ct_pod_recovery():
    """
    In case the of node failures scenarios, in which the selected node is
    running the ceph tools pod, we'll want to wait for the pod recovery

    Returns:
        bool: True in case the ceph tools pod was recovered, False otherwise

    """
    try:
        _ = get_admin_key()
    except CommandFailed as ex:
        logger.info(str(ex))
        if "connection timed out" in str(ex):
            logger.info(
                "Ceph tools box was running on the node that had a failure. "
                "Hence, waiting for a new Ceph tools box pod to spin up"
            )
            wait_for_resource_count_change(
                func_to_use=pod.get_all_pods,
                previous_num=1,
                namespace=config.ENV_DATA["cluster_namespace"],
                timeout=120,
                selector=constants.TOOL_APP_LABEL,
            )
            return True
        else:
            return False
    return True


def label_worker_node(node_list, label_key, label_value):
    """
    Function to label worker node for running app pods on specific worker nodes.

    Args:
        node_list (list): List of node name
        label_key (str): Label_key to be added in worker
        label_value (str): Label_value
    """
    ocp_obj = OCP()
    out = ocp_obj.exec_oc_cmd(
        command=f"label node {' '.join(node_list)} {label_key}={label_value}",
        out_yaml_format=False,
    )
    logger.info(out)


def remove_label_from_worker_node(node_list, label_key):
    """
    Function to remove label from worker node.

    Args:
        node_list (list): List of node name
        label_key (str): Label_key to be remove from worker node
    """
    ocp_obj = OCP()
    out = ocp_obj.exec_oc_cmd(
        command=f"label node {' '.join(node_list)} {label_key}-", out_yaml_format=False
    )
    logger.info(out)


def get_pods_nodes_logs():
    """
    Get logs from all pods and nodes

    Returns:
        dict: node/pod name as key, logs content as value (string)
    """
    all_logs = {}
    all_pods = pod.get_all_pods()
    all_nodes = node.get_node_objs()

    for node_obj in all_nodes:
        node_name = node_obj.name
        log_content = node.get_node_logs(node_name)
        all_logs.update({node_name: log_content})

    for pod_obj in all_pods:
        try:
            pod_name = pod_obj.name
            log_content = pod.get_pod_logs(pod_name)
            all_logs.update({pod_name: log_content})
        except CommandFailed:
            pass

    return all_logs


def get_logs_with_errors(errors=None):
    """
    From logs of all pods and nodes, get only logs
    containing any of specified errors

    Args:
        errors (list): List of errors to look for

    Returns:
        dict: node/pod name as key, logs content as value; may be empty
    """
    all_logs = get_pods_nodes_logs()
    output_logs = {}

    errors_list = constants.CRITICAL_ERRORS

    if errors:
        errors_list = errors_list + errors

    for name, log_content in all_logs.items():
        for error_msg in errors_list:
            if error_msg in log_content:
                logger.debug(f"Found '{error_msg}' in log of {name}")
                output_logs.update({name: log_content})

                log_path = f"{ocsci_log_path()}/{name}.log"
                with open(log_path, "w") as fh:
                    fh.write(log_content)

    return output_logs


def modify_osd_replica_count(resource_name, replica_count):
    """
    Function to modify osd replica count to 0 or 1

    Args:
        resource_name (str): Name of osd i.e, 'rook-ceph-osd-0-c9c4bc7c-bkf4b'
        replica_count (int): osd replica count to be changed to

    Returns:
        bool: True in case if changes are applied. False otherwise

    """
    ocp_obj = ocp.OCP(
        kind=constants.DEPLOYMENT, namespace=defaults.ROOK_CLUSTER_NAMESPACE
    )
    params = f'{{"spec": {{"replicas": {replica_count}}}}}'
    resource_name = "-".join(resource_name.split("-")[0:4])
    return ocp_obj.patch(resource_name=resource_name, params=params)


def modify_deployment_replica_count(deployment_name, replica_count):
    """
    Function to modify deployment replica count,
    i.e to scale up or down deployment

    Args:
        deployment_name (str): Name of deployment
        replica_count (int): replica count to be changed to

    Returns:
        bool: True in case if changes are applied. False otherwise

    """
    ocp_obj = ocp.OCP(
        kind=constants.DEPLOYMENT, namespace=defaults.ROOK_CLUSTER_NAMESPACE
    )
    params = f'{{"spec": {{"replicas": {replica_count}}}}}'
    return ocp_obj.patch(resource_name=deployment_name, params=params)


def collect_performance_stats(dir_name):
    """
    Collect performance stats and saves them in file in json format.

    dir_name (str): directory name to store stats.

    Performance stats include:
        IOPs and throughput percentage of cluster
        CPU, memory consumption of each nodes

    """
    from ocs_ci.ocs.cluster import CephCluster

    log_dir_path = os.path.join(
        os.path.expanduser(config.RUN["log_dir"]),
        f"failed_testcase_ocs_logs_{config.RUN['run_id']}",
        f"{dir_name}_performance_stats",
    )
    if not os.path.exists(log_dir_path):
        logger.info(f"Creating directory {log_dir_path}")
        os.makedirs(log_dir_path)

    performance_stats = {}
    external = config.DEPLOYMENT["external_mode"]
    if external:
        # Skip collecting performance_stats for external mode RHCS cluster
        logging.info("Skipping status collection for external mode")
    else:
        ceph_obj = CephCluster()

        # Get iops and throughput percentage of cluster
        iops_percentage = ceph_obj.get_iops_percentage()
        throughput_percentage = ceph_obj.get_throughput_percentage()

        performance_stats["iops_percentage"] = iops_percentage
        performance_stats["throughput_percentage"] = throughput_percentage

    # ToDo: Get iops and throughput percentage of each nodes

    # Get the cpu and memory of each nodes from adm top
    master_node_utilization_from_adm_top = (
        node.get_node_resource_utilization_from_adm_top(node_type="master")
    )
    worker_node_utilization_from_adm_top = (
        node.get_node_resource_utilization_from_adm_top(node_type="worker")
    )

    # Get the cpu and memory from describe of nodes
    master_node_utilization_from_oc_describe = (
        node.get_node_resource_utilization_from_oc_describe(node_type="master")
    )
    worker_node_utilization_from_oc_describe = (
        node.get_node_resource_utilization_from_oc_describe(node_type="worker")
    )

    performance_stats["master_node_utilization"] = master_node_utilization_from_adm_top
    performance_stats["worker_node_utilization"] = worker_node_utilization_from_adm_top
    performance_stats[
        "master_node_utilization_from_oc_describe"
    ] = master_node_utilization_from_oc_describe
    performance_stats[
        "worker_node_utilization_from_oc_describe"
    ] = worker_node_utilization_from_oc_describe

    file_name = os.path.join(log_dir_path, "performance")
    with open(file_name, "w") as outfile:
        json.dump(performance_stats, outfile)


def validate_pod_oomkilled(
    pod_name, namespace=defaults.ROOK_CLUSTER_NAMESPACE, container=None
):
    """
    Validate pod oomkilled message are found on log

    Args:
        pod_name (str): Name of the pod
        namespace (str): Namespace of the pod
        container (str): Name of the container

    Returns:
        bool : True if oomkill messages are not found on log.
               False Otherwise.

    Raises:
        Assertion if failed to fetch logs

    """
    rc = True
    try:
        pod_log = pod.get_pod_logs(
            pod_name=pod_name, namespace=namespace, container=container, previous=True
        )
        result = pod_log.find("signal: killed")
        if result != -1:
            rc = False
    except CommandFailed as ecf:
        assert (
            f'previous terminated container "{container}" in pod "{pod_name}" not found'
            in str(ecf)
        ), "Failed to fetch logs"

    return rc


def validate_pods_are_running_and_not_restarted(pod_name, pod_restart_count, namespace):
    """
    Validate given pod is in running state and not restarted or re-spinned

    Args:
        pod_name (str): Name of the pod
        pod_restart_count (int): Restart count of pod
        namespace (str): Namespace of the pod

    Returns:
        bool : True if pod is in running state and restart
               count matches the previous one

    """
    ocp_obj = ocp.OCP(kind=constants.POD, namespace=namespace)
    pod_obj = ocp_obj.get(resource_name=pod_name)
    restart_count = (
        pod_obj.get("status").get("containerStatuses")[0].get("restartCount")
    )
    pod_state = pod_obj.get("status").get("phase")
    if pod_state == "Running" and restart_count == pod_restart_count:
        logger.info("Pod is running state and restart count matches with previous one")
        return True
    logger.error(
        f"Pod is in {pod_state} state and restart count of pod {restart_count}"
    )
    logger.info(f"{pod_obj}")
    return False


def calc_local_file_md5_sum(path):
    """
    Calculate and return the MD5 checksum of a local file

    Arguments:
        path(str): The path to the file

    Returns:
        str: The MD5 checksum

    """
    with open(path, "rb") as file_to_hash:
        file_as_bytes = file_to_hash.read()
    return hashlib.md5(file_as_bytes).hexdigest()


def retrieve_default_ingress_crt():
    """
    Copy the default ingress certificate from the router-ca secret
    to the local code runner for usage with boto3.

    """
    default_ingress_crt_b64 = (
        OCP(
            kind="secret",
            namespace="openshift-ingress-operator",
            resource_name="router-ca",
        )
            .get()
            .get("data")
            .get("tls.crt")
    )

    decoded_crt = base64.b64decode(default_ingress_crt_b64).decode("utf-8")

    with open(constants.DEFAULT_INGRESS_CRT_LOCAL_PATH, "w") as crtfile:
        crtfile.write(decoded_crt)


def storagecluster_independent_check():
    """
    Check whether the storagecluster is running in independent mode
    by checking the value of spec.externalStorage.enable

    Returns:
        bool: True if storagecluster is running on external mode False otherwise

    """
    storage_cluster = (
        OCP(kind="StorageCluster", namespace=config.ENV_DATA["cluster_namespace"])
            .get()
            .get("items")[0]
    )

    return bool(
        storage_cluster.get("spec", {}).get("externalStorage", {}).get("enable", False)
    )


def get_pv_size(storageclass=None):
    """
    Get Pv size from requested storageclass

    Args:
        storageclass (str): Name of storageclass

    Returns:
        list: list of pv's size

    """
    return_list = []

    ocp_obj = ocp.OCP(kind=constants.PV)
    pv_objs = ocp_obj.get()["items"]
    for pv_obj in pv_objs:
        if pv_obj["spec"]["storageClassName"] == storageclass:
            return_list.append(pv_obj["spec"]["capacity"]["storage"])
    return return_list


def get_pv_names():
    """
    Get Pv names

    Returns:
        list: list of pv names

    """
    ocp_obj = ocp.OCP(kind=constants.PV)
    pv_objs = ocp_obj.get()["items"]
    return [pv_obj["metadata"]["name"] for pv_obj in pv_objs]


def default_volumesnapshotclass(interface_type):
    """
    Return default VolumeSnapshotClass based on interface_type

    Args:
        interface_type (str): The type of the interface
            (e.g. CephBlockPool, CephFileSystem)

    Returns:
        OCS: VolumeSnapshotClass Instance
    """
    external = config.DEPLOYMENT["external_mode"]
    if interface_type == constants.CEPHBLOCKPOOL:
        resource_name = (
            constants.DEFAULT_EXTERNAL_MODE_VOLUMESNAPSHOTCLASS_RBD
            if external
            else constants.DEFAULT_VOLUMESNAPSHOTCLASS_RBD
        )
    elif interface_type == constants.CEPHFILESYSTEM:
        resource_name = (
            constants.DEFAULT_EXTERNAL_MODE_VOLUMESNAPSHOTCLASS_CEPHFS
            if external
            else constants.DEFAULT_VOLUMESNAPSHOTCLASS_CEPHFS
        )
    base_snapshot_class = OCP(
        kind=constants.VOLUMESNAPSHOTCLASS, resource_name=resource_name
    )
    return OCS(**base_snapshot_class.data)


def get_snapshot_content_obj(snap_obj):
    """
    Get volume snapshot content of a volume snapshot

    Args:
        snap_obj (OCS): OCS instance of kind VolumeSnapshot

    Returns:
        OCS: OCS instance of kind VolumeSnapshotContent

    """
    data = dict()
    data["api_version"] = snap_obj.api_version
    data["kind"] = constants.VOLUMESNAPSHOTCONTENT
    snapcontent = snap_obj.ocp.get(resource_name=snap_obj.name, out_yaml_format=True)[
        "status"
    ]["boundVolumeSnapshotContentName"]
    data["metadata"] = {"name": snapcontent, "namespace": snap_obj.namespace}
    snapcontent_obj = OCS(**data)
    snapcontent_obj.reload()
    return snapcontent_obj


def wait_for_pv_delete(pv_objs):
    """
    Wait for PVs to delete. Delete PVs having ReclaimPolicy 'Retain'

    Args:
        pv_objs (list): OCS instances of kind PersistentVolume

    """
    for pv_obj in pv_objs:
        if (
            pv_obj.data.get("spec").get("persistentVolumeReclaimPolicy")
            == constants.RECLAIM_POLICY_RETAIN
        ):
            wait_for_resource_state(pv_obj, constants.STATUS_RELEASED)
            pv_obj.delete()
        pv_obj.ocp.wait_for_delete(resource_name=pv_obj.name, timeout=180)


@retry(UnexpectedBehaviour, tries=20, delay=10, backoff=1)
def fetch_used_size(cbp_name, exp_val=None):
    """
    Fetch used size in the pool
    Args:
        exp_val(float): Expected size in GB
    Returns:
        float: Used size in GB
    """

    ct_pod = pod.get_ceph_tools_pod()
    rados_status = ct_pod.exec_ceph_cmd(ceph_cmd=f"rados df -p {cbp_name}")
    size_bytes = rados_status["pools"][0]["size_bytes"]

    # Convert size to GB
    used_in_gb = float(format(size_bytes / constants.GB, ".4f"))
    if exp_val and abs(exp_val - used_in_gb) > 1.5:
        raise UnexpectedBehaviour(
            f"Actual {used_in_gb} and expected size {exp_val} not "
            f"matching. Retrying"
        )
    return used_in_gb


def get_full_test_logs_path(cname):
    """
    Getting the full path of the logs file for particular test

    this function use the inspect module to find the name of the caller function, so it need
    to be call once from the main test function.
    the output is in the form of
    ocsci_log_path/<full test file path>/<test filename>/<test class name>/<test function name>

    Args:
        cname (obj): the Class object which was run and called this function

    Return:
        str : full path of the test logs relative to the ocs-ci base logs path

    """

    # the module path relative to ocs-ci base path
    log_file_name = (inspect.stack()[1][1]).replace(f"{os.getcwd()}/", "")

    # The name of the class
    mname = type(cname).__name__

    # the full log path (relative to ocs-ci base path)
    full_log_path = (
        f"{ocsci_log_path()}/{log_file_name}/{mname}/{inspect.stack()[1][3]}"
    )

    return full_log_path


def get_mon_pdb():
    """
    Check for Mon PDB

    Returns:
        disruptions_allowed (int): Count of mon allowed disruption
        min_available_mon (int): Count of minimum mon available
        max_unavailable_mon (int): Count of maximun mon unavailable

    """

    pdb_obj = OCP(
        kind=constants.POD_DISRUPTION_BUDGET,
        resource_name=constants.MON_PDB,
        namespace=defaults.ROOK_CLUSTER_NAMESPACE,
    )

    disruptions_allowed = pdb_obj.get().get("status").get("disruptionsAllowed")
    min_available_mon = pdb_obj.get().get("spec").get("minAvailable")
    max_unavailable_mon = pdb_obj.get().get("spec").get("maxUnavailable")
    return disruptions_allowed, min_available_mon, max_unavailable_mon


def verify_pdb_mon(disruptions_allowed, max_unavailable_mon):
    """
    Compare between the PDB status and the expected PDB status

    Args:
        disruptions_allowed (int): the expected number of disruptions_allowed
        max_unavailable_mon (int): the expected number of max_unavailable_mon

    return:
        bool: True if the expected pdb state equal to actual pdb state, False otherwise

    """
    logging.info("Check mon pdb status")
    mon_pdb = get_mon_pdb()
    result = True
    if disruptions_allowed != mon_pdb[0]:
        result = False
        logger.error(
            f"The expected disruptions_allowed is: {disruptions_allowed}.The actual one is {mon_pdb[0]}"
        )
    if max_unavailable_mon != mon_pdb[2]:
        result = False
        logger.error(
            f"The expected max_unavailable_mon is {max_unavailable_mon}.The actual one is {mon_pdb[2]}"
        )
    return result


@retry(CommandFailed, tries=10, delay=30, backoff=1)
def run_cmd_verify_cli_output(
    cmd=None, expected_output_lst=(), cephtool_cmd=False, debug_node=None
):
    """
    Run command and verify its output

    Args:
        cmd(str): cli command
        expected_output_lst(set): A set of strings that need to be included in the command output.
        cephtool_cmd(bool): command on ceph-tool pod
        debug_node(str): name of node

    Returns:
        bool: True of all strings are included in the command output, False otherwise

    """
    if cephtool_cmd is True:
        tool_pod = pod.get_ceph_tools_pod()
        cmd_start = f"oc rsh -n openshift-storage {tool_pod.name} "
        cmd = f"{cmd_start} {cmd}"
    elif debug_node is not None:
        cmd_start = f"oc debug nodes/{debug_node} -- chroot /host /bin/bash -c "
        cmd = f'{cmd_start} "{cmd}"'

    out = run_cmd(cmd=cmd)
    logger.info(out)
    for expected_output in expected_output_lst:
        if expected_output not in out:
            return False
    return True


def check_rbd_image_used_size(
    pvc_objs, usage_to_compare, rbd_pool=constants.DEFAULT_BLOCKPOOL, expect_match=True
):
    """
    Check if RBD image used size of the PVCs are matching with the given value

    Args:
        pvc_objs (list): List of PVC objects
        usage_to_compare (str): Value of image used size to be compared with actual value. eg: "5GiB"
        rbd_pool (str): Name of the pool
        expect_match (bool): True to verify the used size is equal to 'usage_to_compare' value.
            False to verify the used size is not equal to 'usage_to_compare' value.

    Returns:
        bool: True if the verification is success for all the PVCs, False otherwise

    """
    ct_pod = pod.get_ceph_tools_pod()
    no_match_list = []
    for pvc_obj in pvc_objs:
        rbd_image_name = pvc_obj.get_rbd_image_name
        du_out = ct_pod.exec_ceph_cmd(
            ceph_cmd=f"rbd du -p {rbd_pool} {rbd_image_name}",
            format="",
        )
        used_size = "".join(du_out.strip().split()[-2:])
        if expect_match:
            if usage_to_compare != used_size:
                logger.error(
                    f"Rbd image {rbd_image_name} of PVC {pvc_obj.name} did not meet the expectation."
                    f" Expected used size: {usage_to_compare}. Actual used size: {used_size}. "
                    f"Rbd du out: {du_out}"
                )
                no_match_list.append(pvc_obj.name)
        else:
            if usage_to_compare == used_size:
                logger.error(
                    f"Rbd image {rbd_image_name} of PVC {pvc_obj.name} did not meet the expectation. "
                    f"Expected the used size to be diferent than {usage_to_compare}. "
                    f"Actual used size: {used_size}. Rbd du out: {du_out}"
                )
                no_match_list.append(pvc_obj.name)

    if no_match_list:
        logger.error(
            f"RBD image used size of these PVCs did not meet the expectation - {no_match_list}"
        )
        return False
    return True


def set_configmap_log_level_rook_ceph_operator(value):
    """
    Set ROOK_LOG_LEVEL on configmap of rook-ceph-operator

    Args:
        value (str): type of log

    """
    path = "/data/ROOK_LOG_LEVEL"
    params = f"""[{{"op": "add", "path": "{path}", "value": "{value}"}}]"""
    configmap_obj = OCP(
        kind=constants.CONFIGMAP,
        namespace=constants.OPENSHIFT_STORAGE_NAMESPACE,
        resource_name=constants.ROOK_OPERATOR_CONFIGMAP,
    )
    configmap_obj.patch(params=params, format_type="json")


def get_logs_rook_ceph_operator():
    """
    Get logs from a rook_ceph_operator pod

    Returns:
        str: Output from 'oc get logs rook-ceph-operator command

    """
    logger.info("Get logs from rook_ceph_operator pod")
    rook_ceph_operator_objs = pod.get_operator_pods()
    return pod.get_pod_logs(pod_name=rook_ceph_operator_objs[0].name)


def check_osd_log_exist_on_rook_ceph_operator_pod(
    last_log_date_time_obj, expected_strings=(), unexpected_strings=()
):
    """
    Verify logs contain the expected strings and the logs do not
        contain the unexpected strings

    Args:
        last_log_date_time_obj (datetime obj): type of log
        expected_strings (list): verify the logs contain the expected strings
        unexpected_strings (list): verify the logs do not contain the strings

    Returns:
        bool: True if logs contain the expected strings and the logs do not
        contain the unexpected strings, False otherwise

    """
    logger.info("Respin OSD pod")
    osd_pod_objs = pod.get_osd_pods()
    osd_pod_obj = random.choice(osd_pod_objs)
    osd_pod_obj.delete()
    new_logs = list()
    rook_ceph_operator_logs = get_logs_rook_ceph_operator()
    for line in rook_ceph_operator_logs.splitlines():
        if re.search(r"\d{4}-\d{2}-\d{2}", line):
            log_date_time_obj = datetime.datetime.strptime(
                line[:26], "%Y-%m-%d %H:%M:%S.%f"
            )
            if log_date_time_obj > last_log_date_time_obj:
                new_logs.append(line)
    res_expected = False
    res_unexpected = True
    for new_log in new_logs:
        if all(
            expected_string.lower() in new_log.lower()
            for expected_string in expected_strings
        ):
            res_expected = True
            logger.info(f"{new_log} contain expected strings {expected_strings}")
            break
    for new_log in new_logs:
        if any(
            unexpected_string.lower() in new_log.lower()
            for unexpected_string in unexpected_strings
        ):
            logger.error(f"{new_log} contain unexpected strings {unexpected_strings}")
            res_unexpected = False
            break
    return res_expected & res_unexpected


def get_last_log_time_date():
    """
    Get last log time

    Returns:
        last_log_date_time_obj (datetime obj): type of log

    """
    logger.info("Get last log time")
    rook_ceph_operator_logs = get_logs_rook_ceph_operator()
    for line in rook_ceph_operator_logs.splitlines():
        if re.search(r"\d{4}-\d{2}-\d{2}", line):
            last_log_date_time_obj = datetime.datetime.strptime(
                line[:26], "%Y-%m-%d %H:%M:%S.%f"
            )
    return last_log_date_time_obj


def clear_crash_warning_and_osd_removal_leftovers():
    """
    Clear crash warnings and osd removal leftovers. This function can be used for example,
    after the device replacement test or the node replacement test.
    """
    is_deleted = pod.delete_all_osd_removal_jobs()
    if is_deleted:
        logger.info("Successfully deleted all the ocs-osd-removal jobs")

    is_osd_pods_running = pod.wait_for_pods_to_be_running(
        pod_names=[osd_pod.name for osd_pod in pod.get_osd_pods()], timeout=120
    )

    if not is_osd_pods_running:
        logger.warning("There are still osds down. Can't clear ceph crash warnings")
        return

    is_daemon_recently_crash_warnings = run_cmd_verify_cli_output(
        cmd="ceph health detail",
        expected_output_lst={"HEALTH_WARN", "daemons have recently crashed"},
        cephtool_cmd=True,
    )
    if is_daemon_recently_crash_warnings:
        logger.info("Clear all ceph crash warnings")
        ct_pod = pod.get_ceph_tools_pod()
        ct_pod.exec_ceph_cmd(ceph_cmd="ceph crash archive-all")
    else:
        logger.info("There are no daemon crash warnings")


def get_noobaa_url():
    """
    Get the URL of noobaa console

    Returns:
        str: url of noobaa console

    """
    ocp_obj = OCP(kind=constants.ROUTE, namespace=defaults.ROOK_CLUSTER_NAMESPACE)
    route_obj = ocp_obj.get(resource_name="noobaa-mgmt")
    return route_obj["spec"]["host"]
