from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import logging
import time
import traceback
from typing import Dict, List, Optional, Callable, Tuple

import ray
from ray import cloudpickle
from ray.exceptions import RuntimeEnvSetupError
from ray._private.usage.usage_lib import TagKey, record_extra_usage_tag
from ray._private.utils import import_attr
from ray.serve.config import DeploymentConfig
from ray.serve.exceptions import RayServeException

from ray.serve._private.common import (
    DeploymentStatus,
    DeploymentStatusInfo,
    ApplicationStatusInfo,
    ApplicationStatus,
    EndpointInfo,
    DeploymentInfo,
)
from ray.serve._private.constants import (
    SERVE_LOGGER_NAME,
    DEPLOYMENT_NAME_PREFIX_SEPARATOR,
)
from ray.serve._private.deploy_utils import (
    deploy_args_to_deployment_info,
    get_app_code_version,
)
from ray.serve._private.deployment_state import DeploymentStateManager
from ray.serve._private.endpoint_state import EndpointState
from ray.serve._private.storage.kv_store import KVStoreBase
from ray.serve._private.utils import (
    check_obj_ref_ready_nowait,
    override_runtime_envs_except_env_vars,
    DEFAULT,
)
from ray.serve.schema import DeploymentDetails, ServeApplicationSchema
from ray.types import ObjectRef

logger = logging.getLogger(SERVE_LOGGER_NAME)

CHECKPOINT_KEY = "serve-application-state-checkpoint"


class BuildAppStatus(Enum):
    """Status of the build application task."""

    NO_TASK_STARTED = 1
    IN_PROGRESS = 2
    SUCCEEDED = 3
    FAILED = 4


@dataclass
class BuildAppTaskInfo:
    """Stores info on the current in-progress build app task.

    We use a class instead of only storing the task object ref because
    when a new config is deployed, there can be an outdated in-progress
    build app task. We attach the code version to the task info to
    distinguish outdated build app tasks.
    """

    obj_ref: ObjectRef
    code_version: str


@dataclass
class ApplicationTargetState:
    """Defines target state of application.

    Target state can become inconsistent if the code version doesn't
    match that of the config. When that happens, a new build app task
    should be kicked off to reconcile the inconsistency.

    deployment_infos: Map of deployment name to deployment info. This is
      - None if a config was deployed but the app hasn't finished
        building yet
      - An empty dict if the app is deleting
    code_version: Code version of all deployments in target state. None
        if application was deployed through serve.run
    config: application config deployed by user. None if application was
        deployed through serve.run
    deleting: whether the application is being deleted.
    """

    deployment_infos: Optional[Dict[str, DeploymentInfo]]
    code_version: Optional[str]
    config: Optional[ServeApplicationSchema]
    deleting: bool


class ApplicationState:
    """Manage single application states with all operations"""

    def __init__(
        self,
        name: str,
        deployment_state_manager: DeploymentStateManager,
        endpoint_state: EndpointState,
        save_checkpoint_func: Callable,
    ):
        """
        Args:
            name: Application name.
            deployment_state_manager: State manager for all deployments
                in the cluster.
            endpoint_state: State manager for endpoints in the system.
            save_checkpoint_func: Function that can be called to write
                a checkpoint of the application state. This should be
                called in self._set_target_state() before actually
                setting the target state so that the controller can
                properly recover application states if it crashes.
        """

        self._name = name
        self._status_msg = ""
        self._deployment_state_manager = deployment_state_manager
        self._endpoint_state = endpoint_state
        self._route_prefix = None
        self._docs_path = None

        self._status: ApplicationStatus = ApplicationStatus.DEPLOYING
        self._deployment_timestamp = time.time()

        self._build_app_task_info: Optional[BuildAppTaskInfo] = None
        # Before a deploy app task finishes, we don't know what the
        # target deployments are, so set deployment_infos=None
        self._target_state: ApplicationTargetState = ApplicationTargetState(
            deployment_infos=None,
            code_version=None,
            config=None,
            deleting=False,
        )
        self._save_checkpoint_func = save_checkpoint_func

    @property
    def route_prefix(self) -> Optional[str]:
        return self._route_prefix

    @property
    def docs_path(self) -> Optional[str]:
        return self._docs_path

    @property
    def status(self) -> ApplicationStatus:
        """Status of the application.

        DEPLOYING: The deploy task is still running, or the deployments
            have started deploying but aren't healthy yet.
        RUNNING: All deployments are healthy.
        DEPLOY_FAILED: The deploy task failed or one or more deployments
            became unhealthy in the process of deploying
        UNHEALTHY: While the application was running, one or more
            deployments transition from healthy to unhealthy.
        DELETING: Application and its deployments are being deleted.
        """
        return self._status

    @property
    def deployment_timestamp(self) -> int:
        return self._deployment_timestamp

    @property
    def build_app_obj_ref(self) -> Optional[ObjectRef]:
        return self._build_app_obj_ref

    @property
    def target_deployments(self) -> List[str]:
        """List of target deployment names in application."""
        if self._target_state.deployment_infos is None:
            return []
        return list(self._target_state.deployment_infos.keys())

    def recover_target_state_from_checkpoint(
        self, checkpoint_data: ApplicationTargetState
    ):
        logger.info(
            f"Recovering target state for application '{self._name}' from checkpoint."
        )
        self._set_target_state(
            checkpoint_data.deployment_infos,
            checkpoint_data.code_version,
            checkpoint_data.config,
            checkpoint_data.deleting,
        )

    def _set_target_state(
        self,
        deployment_infos: Optional[Dict[str, DeploymentInfo]] = None,
        code_version: str = None,
        target_config: Optional[ServeApplicationSchema] = None,
        deleting: bool = False,
    ):
        """Set application target state.

        While waiting for deploy task to finish, this should be
            (None, False)
        When deploy task has finished and during normal operation, this should be
            (target_deployments, False)
        When a request to delete the application has been received, this should be
            ({}, True)
        """

        if deleting:
            self._update_status(ApplicationStatus.DELETING)
        else:
            self._update_status(ApplicationStatus.DEPLOYING)

        target_state = ApplicationTargetState(
            deployment_infos, code_version, target_config, deleting
        )

        # Checkpoint ahead, so that if the controller crashes before we
        # write to the target state, the target state will be recovered
        # after the controller recovers
        self._save_checkpoint_func(writeahead_checkpoints={self._name: target_state})
        # Set target state
        self._target_state = target_state

    def _set_target_state_deleting(self):
        """Set target state to deleting.

        Wipes the target deployment infos, code version, and config.
        """
        self._set_target_state(dict(), None, None, True)

    def _set_target_state_deployment_infos(
        self, deployment_infos: Optional[Dict[str, DeploymentInfo]]
    ):
        """Updates only the target deployment infos."""
        self._set_target_state(
            deployment_infos=deployment_infos,
            code_version=self._target_state.code_version,
            target_config=self._target_state.config,
        )

    def _set_target_state_config(self, target_config: Optional[ServeApplicationSchema]):
        """Updates only the target config."""
        self._set_target_state(
            deployment_infos=self._target_state.deployment_infos,
            code_version=self._target_state.code_version,
            target_config=target_config,
        )

    def _delete_deployment(self, name):
        self._endpoint_state.delete_endpoint(name)
        self._deployment_state_manager.delete_deployment(name)

    def delete(self):
        """Delete the application"""
        logger.info(
            f"Deleting application '{self._name}'",
            extra={"log_to_stderr": False},
        )
        self._set_target_state_deleting()

    def is_deleted(self) -> bool:
        """Check whether the application is already deleted.

        For an application to be considered deleted, the target state has to be set to
        deleting and all deployments have to be deleted.
        """
        return self._target_state.deleting and len(self._get_live_deployments()) == 0

    def apply_deployment_info(
        self, deployment_name: str, deployment_info: DeploymentInfo
    ) -> None:
        """Deploys a deployment in the application."""
        route_prefix = deployment_info.route_prefix
        if route_prefix is not None and not route_prefix.startswith("/"):
            raise RayServeException(
                f'Invalid route prefix "{route_prefix}", it must start with "/"'
            )

        self._deployment_state_manager.deploy(deployment_name, deployment_info)

        if deployment_info.route_prefix is not None:
            config = deployment_info.deployment_config
            self._endpoint_state.update_endpoint(
                deployment_name,
                EndpointInfo(
                    route=deployment_info.route_prefix,
                    app_name=self._name,
                    app_is_cross_language=config.is_cross_language,
                ),
            )
        else:
            self._endpoint_state.delete_endpoint(deployment_name)

    def apply_deployment_args(
        self,
        deployment_params: List[Dict],
        code_version: str = None,
    ) -> None:
        """Set the list of deployment infos in application target state.

        Args:
            deployment_params: list of deployment parameters, including
                all deployment information.
            code_version: the application code version associated with
                the set of deployments.

        Raises:
            RayServeException: If there is more than one deployment with
                a non-null route prefix or docs path.
        """
        # Makes sure that at most one deployment has a non-null route
        # prefix and docs path.
        num_route_prefixes = 0
        num_docs_paths = 0
        for deploy_param in deployment_params:
            if deploy_param.get("route_prefix") is not None:
                self._route_prefix = deploy_param["route_prefix"]
                num_route_prefixes += 1

            if deploy_param.get("docs_path") is not None:
                self._docs_path = deploy_param["docs_path"]
                num_docs_paths += 1
        if num_route_prefixes > 1:
            raise RayServeException(
                f'Found multiple route prefixes from application "{self._name}",'
                " Please specify only one route prefix for the application "
                "to avoid this issue."
            )
        # NOTE(zcin) This will not catch multiple FastAPI deployments in the application
        # if user sets the docs path to None in their FastAPI app.
        if num_docs_paths > 1:
            raise RayServeException(
                f'Found multiple deployments in application "{self._name}" that have '
                "a docs path. This may be due to using multiple FastAPI deployments "
                "in your application. Please only include one deployment with a docs "
                "path in your application to avoid this issue."
            )

        for params in deployment_params:
            params["deployment_name"] = params.pop("name")
            params["app_name"] = self._name

        deployment_infos = {
            params["deployment_name"]: deploy_args_to_deployment_info(**params)
            for params in deployment_params
        }
        self._set_target_state(
            deployment_infos=deployment_infos,
            code_version=code_version,
            target_config=self._target_state.config,
        )

    def deploy_config(
        self, config: ServeApplicationSchema, deployment_time: int
    ) -> None:
        """Deploys an application config."""
        self._deployment_timestamp = deployment_time
        self._set_target_state_config(config)

    def _get_live_deployments(self) -> List[str]:
        return self._deployment_state_manager.get_deployments_in_application(self._name)

    def _determine_app_status(self) -> Tuple[ApplicationStatus, str]:
        """Check deployment statuses and target state, and determine the
        corresponding application status.

        Returns:
            Status (ApplicationStatus):
                RUNNING: all deployments are healthy.
                DEPLOYING: there is one or more updating deployments,
                    and there are no unhealthy deployments.
                DEPLOY_FAILED: one or more deployments became unhealthy
                    while the application was deploying.
                UNHEALTHY: one or more deployments became unhealthy
                    while the application was running.
                DELETING: the application is being deleted.
            Error message (str):
                Non-empty string if status is DEPLOY_FAILED or UNHEALTHY
        """

        if self._target_state.deleting:
            return ApplicationStatus.DELETING, ""

        num_healthy_deployments = 0
        unhealthy_deployment_names = []

        for deployment_status in self.get_deployments_statuses():
            if deployment_status.status == DeploymentStatus.UNHEALTHY:
                unhealthy_deployment_names.append(deployment_status.name)
            if deployment_status.status == DeploymentStatus.HEALTHY:
                num_healthy_deployments += 1

        if num_healthy_deployments == len(self.target_deployments):
            return ApplicationStatus.RUNNING, ""
        elif len(unhealthy_deployment_names):
            status_msg = f"The deployments {unhealthy_deployment_names} are UNHEALTHY."
            if self._status in [
                ApplicationStatus.DEPLOYING,
                ApplicationStatus.DEPLOY_FAILED,
            ]:
                return ApplicationStatus.DEPLOY_FAILED, status_msg
            else:
                return ApplicationStatus.UNHEALTHY, status_msg
        else:
            return ApplicationStatus.DEPLOYING, ""

    def _start_or_reconcile_build_app_task(self) -> Tuple[Tuple, BuildAppStatus, str]:
        """If necessary, start or reconcile the in-progress build task.

        If the current code version is inconsistent with that of the
        target config, either start a new build task or reconcile an
        in-progress one. Note self._build_app_task_info is reset when
        task finishes, regardless of whether it finished successfully

        Returns:
            Deploy arguments (Tuple[List, str]):
                The deploy arguments returned from the build app task
                and their code version.
            Status (BuildAppStatus):
                NO_TASK_STARTED:
                SUCCEEDED: task finished successfully.
                FAILED: an error occurred during execution of build app task
                IN_PROGRESS: task hasn't finished yet.
            Error message (str):
                Non-empty string if status is DEPLOY_FAILED or UNHEALTHY
        """
        if self._target_state.config is None:
            return None, BuildAppStatus.NO_TASK_STARTED, ""

        config_version = get_app_code_version(self._target_state.config)
        if config_version == self._target_state.code_version:
            return None, BuildAppStatus.NO_TASK_STARTED, ""

        # If there is a non-null target config, and the current code
        # version is out of sync with that target config, we need to
        # rebuild the application with the new target config
        if (
            self._build_app_task_info is None
            or self._build_app_task_info.code_version != config_version
        ):
            # If there is an in progress build task, cancel it.
            if self._build_app_task_info:
                logger.info(
                    f'Received new config for application "{self._name}". '
                    "Cancelling previous request."
                )
                ray.cancel(self._build_app_task_info.obj_ref)

            # Halt reconciliation of target deployments
            self._set_target_state_deployment_infos(None)

            # Kick off new build app task
            logger.info(
                f"Starting build_serve_application task for application {self._name}."
            )
            build_app_obj_ref = build_serve_application.options(
                runtime_env=self._target_state.config.runtime_env
            ).remote(
                self._target_state.config.import_path,
                self._target_state.config.deployment_names,
                config_version,
                self._target_state.config.name,
                self._target_state.config.args,
            )
            self._build_app_task_info = BuildAppTaskInfo(
                build_app_obj_ref, config_version
            )
        elif check_obj_ref_ready_nowait(self._build_app_task_info.obj_ref):
            build_app_obj_ref = self._build_app_task_info.obj_ref
            self._build_app_task_info = None
            try:
                args, err = ray.get(build_app_obj_ref)
                if err is None:
                    logger.info(f"Deploy task for app '{self._name}' ran successfully.")
                    return (args, config_version), BuildAppStatus.SUCCEEDED, ""
                else:
                    error_msg = (
                        f"Deploying app '{self._name}' failed with "
                        f"exception:\n{err}"
                    )
                    logger.warning(error_msg)
                    return None, BuildAppStatus.FAILED, error_msg
            except RuntimeEnvSetupError:
                error_msg = (
                    f"Runtime env setup for app '{self._name}' failed:\n"
                    + traceback.format_exc()
                )
                logger.warning(error_msg)
                return None, BuildAppStatus.FAILED, error_msg
            except Exception:
                error_msg = (
                    f"Unexpected error occured while deploying application "
                    f"'{self._name}': \n{traceback.format_exc()}"
                )
                logger.warning(error_msg)
                return None, BuildAppStatus.FAILED, error_msg

        return None, BuildAppStatus.IN_PROGRESS, ""

    def _reconcile_target_deployments(self) -> None:
        """Reconcile target deployments in application target state.

        Ensure each deployment is running on up-to-date info, and
        remove outdated deployments from the application.
        """

        # Apply override options from target config to each deployment
        overrided_infos = override_deployment_info(
            self._name,
            self._target_state.deployment_infos,
            self._target_state.config,
        )
        # Set target state for each deployment
        for deployment_name, info in overrided_infos.items():
            self.apply_deployment_info(deployment_name, info)

        # Delete outdated deployments
        for deployment_name in self._get_live_deployments():
            if deployment_name not in self.target_deployments:
                self._delete_deployment(deployment_name)

    def update(self) -> bool:
        """Attempts to reconcile this application to match its target state.

        Updates the application status and status message based on the
        current state of the system.

        Returns:
            A boolean indicating whether the application is ready to be
            deleted.
        """

        args, task_status, msg = self._start_or_reconcile_build_app_task()
        if task_status == BuildAppStatus.SUCCEEDED:
            self.apply_deployment_args(*args)
        elif task_status == BuildAppStatus.FAILED:
            self._update_status(ApplicationStatus.DEPLOY_FAILED, msg)

        # If we're waiting on the build app task to finish, we don't
        # have info on what the target list of deployments is, so don't
        # perform reconciliation or check on deployment statuses
        if self._target_state.deployment_infos is not None:
            self._reconcile_target_deployments()

            status, status_msg = self._determine_app_status()
            self._update_status(status, status_msg)

        # Check if app is ready to be deleted
        if self._target_state.deleting:
            return self.is_deleted()
        return False

    def get_checkpoint_data(self) -> ApplicationTargetState:
        return self._target_state

    def get_deployment(self, name: str) -> DeploymentInfo:
        """Get deployment info for deployment by name."""
        return self._deployment_state_manager.get_deployment(name)

    def get_deployments_statuses(self) -> List[DeploymentStatusInfo]:
        """Return all deployment status information"""
        return self._deployment_state_manager.get_deployment_statuses(
            self.target_deployments
        )

    def get_application_status_info(self) -> ApplicationStatusInfo:
        """Return the application status information"""
        return ApplicationStatusInfo(
            self._status,
            message=self._status_msg,
            deployment_timestamp=self._deployment_timestamp,
        )

    def list_deployment_details(self) -> Dict[str, DeploymentDetails]:
        """Gets detailed info on all live deployments in this application.
        (Does not include deleted deployments.)

        Returns:
            A dictionary of deployment infos. The set of deployment info returned
            may not be the full list of deployments that are part of the application.
            This can happen when the application is still deploying and bringing up
            deployments, or when the application is deleting and some deployments have
            been deleted.
        """
        details = {
            name: self._deployment_state_manager.get_deployment_details(name)
            for name in self.target_deployments
        }
        return {k: v for k, v in details.items() if v is not None}

    def _update_status(self, status: ApplicationStatus, status_msg: str = "") -> None:
        self._status = status
        self._status_msg = status_msg


class ApplicationStateManager:
    def __init__(
        self,
        deployment_state_manager: DeploymentStateManager,
        endpoint_state: EndpointState,
        kv_store: KVStoreBase,
    ):
        self._deployment_state_manager = deployment_state_manager
        self._endpoint_state = endpoint_state
        self._kv_store = kv_store
        self._application_states: Dict[str, ApplicationState] = dict()
        self._recover_from_checkpoint()

    def _recover_from_checkpoint(self):
        checkpoint = self._kv_store.get(CHECKPOINT_KEY)
        if checkpoint is not None:
            application_state_info = cloudpickle.loads(checkpoint)

            for app_name, checkpoint_data in application_state_info.items():
                app_state = ApplicationState(
                    app_name,
                    self._deployment_state_manager,
                    self._endpoint_state,
                    self._save_checkpoint_func,
                )
                app_state.recover_target_state_from_checkpoint(checkpoint_data)
                self._application_states[app_name] = app_state

    def delete_application(self, name: str) -> None:
        """Delete application by name"""
        if name not in self._application_states:
            return
        self._application_states[name].delete()

    def apply_deployment_args(self, name: str, deployment_args: List[Dict]) -> None:
        """Apply list of deployment arguments to application target state.

        This function should only be called if the app is being deployed
        through serve.run instead of from a config.

        Args:
            name: application name
            deployment_args_list: arguments for deploying a list of deployments.

        Raises:
            RayServeException: If the list of deployments is trying to
                use a route prefix that is already used by another application
        """

        # Make sure route_prefix is not being used by other application.
        live_route_prefixes: Dict[str, str] = {
            self._application_states[app_name].route_prefix: app_name
            for app_name, app_state in self._application_states.items()
            if app_state.route_prefix is not None
            and not app_state.status == ApplicationStatus.DELETING
            and name != app_name
        }

        for deploy_param in deployment_args:
            deploy_app_prefix = deploy_param.get("route_prefix")
            if deploy_app_prefix in live_route_prefixes:
                raise RayServeException(
                    f"Prefix {deploy_app_prefix} is being used by application "
                    f'"{live_route_prefixes[deploy_app_prefix]}".'
                    f' Failed to deploy application "{name}".'
                )

        if name not in self._application_states:
            self._application_states[name] = ApplicationState(
                name,
                self._deployment_state_manager,
                self._endpoint_state,
                self._save_checkpoint_func,
            )
        record_extra_usage_tag(
            TagKey.SERVE_NUM_APPS, str(len(self._application_states))
        )
        self._application_states[name].apply_deployment_args(deployment_args)

    def deploy_config(
        self,
        name: str,
        app_config: ServeApplicationSchema,
        deployment_time: float = 0,
    ) -> None:
        """Deploy application from config."""

        if name not in self._application_states:
            self._application_states[name] = ApplicationState(
                name,
                self._deployment_state_manager,
                endpoint_state=self._endpoint_state,
                save_checkpoint_func=self._save_checkpoint_func,
            )
        record_extra_usage_tag(
            TagKey.SERVE_NUM_APPS, str(len(self._application_states))
        )
        self._application_states[name].deploy_config(
            app_config,
            deployment_time,
        )

    def get_deployments(self, app_name: str) -> List[str]:
        """Return all deployment names by app name"""
        if app_name not in self._application_states:
            return []
        return self._application_states[app_name].target_deployments

    def get_deployments_statuses(self, app_name: str) -> List[DeploymentStatusInfo]:
        """Return all deployment statuses by app name"""
        if app_name not in self._application_states:
            return []
        return self._application_states[app_name].get_deployments_statuses()

    def get_app_status(self, name: str) -> ApplicationStatus:
        if name not in self._application_states:
            return ApplicationStatus.NOT_STARTED

        return self._application_states[name].status

    def get_app_status_info(self, name: str) -> ApplicationStatusInfo:
        if name not in self._application_states:
            return ApplicationStatusInfo(
                ApplicationStatus.NOT_STARTED,
                message=f"Application {name} doesn't exist",
                deployment_timestamp=0,
            )
        return self._application_states[name].get_application_status_info()

    def get_deployment_timestamp(self, name: str) -> float:
        if name not in self._application_states:
            return -1
        return self._application_states[name].deployment_timestamp

    def get_docs_path(self, app_name: str) -> Optional[str]:
        return self._application_states[app_name].docs_path

    def get_route_prefix(self, name: str) -> Optional[str]:
        return self._application_states[name].route_prefix

    def list_app_statuses(self) -> Dict[str, ApplicationStatusInfo]:
        """Return a dictionary with {app name: application info}"""
        return {
            name: self._application_states[name].get_application_status_info()
            for name in self._application_states
        }

    def list_deployment_details(self, name: str) -> Dict[str, DeploymentDetails]:
        """Gets detailed info on all deployments in specified application."""
        if name not in self._application_states:
            return {}
        return self._application_states[name].list_deployment_details()

    def update(self):
        """Update each application state"""
        apps_to_be_deleted = []
        for name, app in self._application_states.items():
            ready_to_be_deleted = app.update()
            if ready_to_be_deleted:
                apps_to_be_deleted.append(name)

        if len(apps_to_be_deleted) > 0:
            for app_name in apps_to_be_deleted:
                del self._application_states[app_name]
            record_extra_usage_tag(
                TagKey.SERVE_NUM_APPS, str(len(self._application_states))
            )

    def shutdown(self) -> None:
        for app_state in self._application_states.values():
            app_state.delete()

    def is_ready_for_shutdown(self) -> bool:
        """Return whether all applications have shut down.

        Iterate through all application states and check if all their applications
        are deleted.
        """
        return all(
            app_state.is_deleted() for app_state in self._application_states.values()
        )

    def _save_checkpoint_func(
        self, *, writeahead_checkpoints: Optional[Dict[str, ApplicationTargetState]]
    ) -> None:
        """Write a checkpoint of all application states."""

        application_state_info = {
            app_name: app_state.get_checkpoint_data()
            for app_name, app_state in self._application_states.items()
        }

        if writeahead_checkpoints is not None:
            application_state_info.update(writeahead_checkpoints)

        self._kv_store.put(
            CHECKPOINT_KEY,
            cloudpickle.dumps(application_state_info),
        )


@ray.remote(num_cpus=0, max_calls=1)
def build_serve_application(
    import_path: str,
    config_deployments: List[str],
    code_version: str,
    name: str,
    args: Dict,
) -> Tuple[List[Dict], Optional[str]]:
    """Import and build a Serve application.

    Args:
        import_path: import path to top-level bound deployment.
        config_deployments: list of deployment names specified in config
            with deployment override options. This is used to check that
            all deployments specified in the config are valid.
        code_version: code version inferred from app config. All
            deployment versions are set to this code version.
        name: application name. If specified, application will be deployed
            without removing existing applications.
        args: Arguments to be passed to the application builder.
    Returns:
        Deploy arguments: a list of deployment arguments if application
            was built successfully, otherwise None.
        Error message: a string if an error was raised, otherwise None.
    """
    try:
        from ray.serve.api import build
        from ray.serve._private.api import call_app_builder_with_args_if_necessary
        from ray.serve.built_application import _get_deploy_args_from_built_app

        # Import and build the application.
        app = call_app_builder_with_args_if_necessary(import_attr(import_path), args)
        app = build(app, name)

        # Check that all deployments specified in config are valid
        for deployment_name in config_deployments:
            unique_deployment_name = (
                (name + DEPLOYMENT_NAME_PREFIX_SEPARATOR) if len(name) else ""
            ) + deployment_name
            if unique_deployment_name not in app.deployments:
                raise KeyError(
                    f'There is no deployment named "{deployment_name}" in the '
                    f'application "{name}".'
                )

        # Set code version and runtime env for each deployment
        for deployment_name in app.deployments:
            app.deployments[deployment_name].set_options(
                version=code_version,
                _internal=True,
            )

        return _get_deploy_args_from_built_app(app), None
    except KeyboardInterrupt:
        # Error is raised when this task is canceled with ray.cancel(), which
        # happens when deploy_apps() is called.
        logger.info("Existing config deployment request terminated.")
        return None, None
    except Exception as e:
        return None, repr(e)


def override_deployment_info(
    app_name: str,
    deployment_infos: Dict[str, DeploymentInfo],
    override_config: Optional[ServeApplicationSchema],
) -> Dict[str, DeploymentInfo]:
    """Override deployment infos with options from app config.

    Args:
        app_name: application name
        deployment_infos: deployment info loaded from code
        override_config: application config deployed by user with
            options to override those loaded from code.

    Returns: the updated deployment infos.
    """

    deployment_infos = deepcopy(deployment_infos)
    if override_config is None:
        return deployment_infos

    config_dict = override_config.dict(exclude_unset=True)
    deployment_override_options = config_dict.get("deployments", [])

    # Override options for each deployment listed in the config.
    for options in deployment_override_options:
        deployment_name = options["name"]
        unique_deployment_name = (
            (app_name + DEPLOYMENT_NAME_PREFIX_SEPARATOR) if len(app_name) else ""
        ) + deployment_name
        info = deployment_infos[unique_deployment_name]

        if (
            info.deployment_config.autoscaling_config is not None
            and info.deployment_config.max_concurrent_queries
            < info.deployment_config.autoscaling_config.target_num_ongoing_requests_per_replica  # noqa: E501
        ):
            logger.warning(
                "Autoscaling will never happen, "
                "because 'max_concurrent_queries' is less than "
                "'target_num_ongoing_requests_per_replica' now."
            )

        # What to pass to info.update
        override_options = dict()

        # Override route prefix if specified in deployment config
        deployment_route_prefix = options.pop("route_prefix", DEFAULT.VALUE)
        if deployment_route_prefix is not DEFAULT.VALUE:
            override_options["route_prefix"] = deployment_route_prefix

        # Override is_driver_deployment if specified in deployment config
        is_driver_deployment = options.pop("is_driver_deployment", None)
        if is_driver_deployment is not None:
            override_options["is_driver_deployment"] = is_driver_deployment

        # Merge app-level and deployment-level runtime_envs.
        replica_config = info.replica_config
        app_runtime_env = override_config.runtime_env
        if "ray_actor_options" in options:
            # If specified, get ray_actor_options from config
            override_actor_options = options.pop("ray_actor_options", {})
        else:
            # Otherwise, get options from application code (and default to {}
            # if the code sets options to None).
            override_actor_options = replica_config.ray_actor_options or {}

        merged_env = override_runtime_envs_except_env_vars(
            app_runtime_env, override_actor_options.get("runtime_env", {})
        )
        override_actor_options.update({"runtime_env": merged_env})
        replica_config.update_ray_actor_options(override_actor_options)
        override_options["replica_config"] = replica_config

        # Override deployment config options
        original_options = info.deployment_config.dict()
        options.pop("name", None)
        original_options.update(options)
        override_options["deployment_config"] = DeploymentConfig(**original_options)

        deployment_infos[unique_deployment_name] = info.update(**override_options)

    # Overwrite ingress route prefix
    app_route_prefix = config_dict.get("route_prefix", DEFAULT.VALUE)
    for deployment in list(deployment_infos.values()):
        if (
            app_route_prefix is not DEFAULT.VALUE
            and deployment.route_prefix is not None
        ):
            deployment.route_prefix = app_route_prefix

    return deployment_infos
