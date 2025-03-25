from __future__ import annotations

import collections.abc
import inspect
from dataclasses import dataclass, field, fields
from os import getenv
from types import GeneratorType
from typing import Any, Callable, Dict, List, Optional, cast
from uuid import uuid4

from pydantic import BaseModel

from agno.agent import Agent
from agno.media import AudioArtifact, ImageArtifact, VideoArtifact
from agno.memory.workflow import WorkflowMemory, WorkflowRun
from agno.run.response import RunEvent, RunResponse  # noqa: F401
from agno.storage.base import Storage
from agno.storage.session.workflow import WorkflowSession
from agno.utils.common import nested_model_dump
from agno.utils.log import log_debug, logger, set_log_level_to_debug, set_log_level_to_info
from agno.utils.merge_dict import merge_dictionaries


@dataclass(init=False)
class Workflow:
    # --- Workflow settings ---
    # Workflow name
    name: Optional[str] = None
    # Workflow UUID (autogenerated if not set)
    workflow_id: Optional[str] = None
    # Workflow description (only shown in the UI)
    description: Optional[str] = None

    # --- User settings ---
    # ID of the user interacting with this workflow
    user_id: Optional[str] = None

    # -*- Session settings
    # Session UUID (autogenerated if not set)
    session_id: Optional[str] = None
    # Session name
    session_name: Optional[str] = None
    # Session state stored in the database
    session_state: Dict[str, Any] = field(default_factory=dict)

    # --- Workflow Memory ---
    memory: Optional[WorkflowMemory] = None

    # --- Workflow Storage ---
    storage: Optional[Storage] = None
    # Extra data stored with this workflow
    extra_data: Optional[Dict[str, Any]] = None

    # --- Debug & Monitoring ---
    # Enable debug logs
    debug_mode: bool = False
    # monitoring=True logs Workflow information to agno.com for monitoring
    monitoring: bool = field(default_factory=lambda: getenv("AGNO_MONITOR", "false").lower() == "true")
    # telemetry=True logs minimal telemetry for analytics
    # This helps us improve the Workflow and provide better support
    telemetry: bool = field(default_factory=lambda: getenv("AGNO_TELEMETRY", "true").lower() == "true")

    # --- Run Info: DO NOT SET ---
    run_id: Optional[str] = None
    run_input: Optional[Dict[str, Any]] = None
    run_response: Optional[RunResponse] = None
    # Images generated during this session
    images: Optional[List[ImageArtifact]] = None
    # Videos generated during this session
    videos: Optional[List[VideoArtifact]] = None
    # Audio generated during this session
    audio: Optional[List[AudioArtifact]] = None

    def __init__(
        self,
        *,
        name: Optional[str] = None,
        workflow_id: Optional[str] = None,
        description: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        session_name: Optional[str] = None,
        session_state: Optional[Dict[str, Any]] = None,
        memory: Optional[WorkflowMemory] = None,
        storage: Optional[Storage] = None,
        extra_data: Optional[Dict[str, Any]] = None,
        debug_mode: bool = False,
        monitoring: bool = False,
        telemetry: bool = True,
    ):
        self.name = name or self.__class__.__name__
        self.workflow_id = workflow_id
        self.description = description or self.__class__.description

        self.user_id = user_id

        self.session_id = session_id
        self.session_name = session_name
        self.session_state: Dict[str, Any] = session_state or {}

        self.memory = memory
        self.storage = storage
        self.extra_data = extra_data

        self.debug_mode = debug_mode
        self.monitoring = monitoring
        self.telemetry = telemetry

        self.run_id = None
        self.run_input = None
        self.run_response = None
        self.images = None
        self.videos = None
        self.audio = None

        self.workflow_session: Optional[WorkflowSession] = None

        # Private attributes to store the run method and its parameters
        # The run function provided by the subclass
        self._subclass_run: Optional[Callable] = None
        # Parameters of the run function
        self._run_parameters: Optional[Dict[str, Any]] = None
        # Return type of the run function
        self._run_return_type: Optional[str] = None

        self.update_run_method()

        self.__post_init__()

    def __post_init__(self):
        for field_name, value in self.__class__.__dict__.items():
            if isinstance(value, Agent):
                value.session_id = self.session_id

    def run(self, **kwargs: Any):
        logger.error(f"{self.__class__.__name__}.run() method not implemented.")
        return

    def run_workflow(self, **kwargs: Any):
        """Run the Workflow"""

        # Set mode, debug, workflow_id, session_id, initialize memory
        self.set_storage_mode()
        self.set_debug()
        self.set_workflow_id()
        self.set_session_id()
        self.initialize_memory()
        self.memory = cast(WorkflowMemory, self.memory)

        # Create a run_id
        self.run_id = str(uuid4())

        # Set run_input, run_response
        self.run_input = kwargs
        self.run_response = RunResponse(run_id=self.run_id, session_id=self.session_id, workflow_id=self.workflow_id)

        # Read existing session from storage
        self.read_from_storage()

        # Update the session_id for all Agent instances
        self.update_agent_session_ids()

        log_debug(f"*********** Workflow Run Start: {self.run_id} ***********")
        try:
            self._subclass_run = cast(Callable, self._subclass_run)
            result = self._subclass_run(**kwargs)
        except Exception as e:
            logger.error(f"Workflow.run() failed: {e}")
            raise e

        # The run_workflow() method handles both Iterator[RunResponse] and RunResponse
        # Case 1: The run method returns an Iterator[RunResponse]
        if isinstance(result, (GeneratorType, collections.abc.Iterator)):
            # Initialize the run_response content
            self.run_response.content = ""

            def result_generator():
                self.run_response = cast(RunResponse, self.run_response)
                self.memory = cast(WorkflowMemory, self.memory)
                for item in result:
                    if isinstance(item, RunResponse):
                        # Update the run_id, session_id and workflow_id of the RunResponse
                        item.run_id = self.run_id
                        item.session_id = self.session_id
                        item.workflow_id = self.workflow_id

                        # Update the run_response with the content from the result
                        if item.content is not None and isinstance(item.content, str):
                            self.run_response.content += item.content
                    else:
                        logger.warning(f"Workflow.run() should only yield RunResponse objects, got: {type(item)}")
                    yield item

                # Add the run to the memory
                self.memory.add_run(WorkflowRun(input=self.run_input, response=self.run_response))
                # Write this run to the database
                self.write_to_storage()
                log_debug(f"*********** Workflow Run End: {self.run_id} ***********")

            return result_generator()
        # Case 2: The run method returns a RunResponse
        elif isinstance(result, RunResponse):
            # Update the result with the run_id, session_id and workflow_id of the workflow run
            result.run_id = self.run_id
            result.session_id = self.session_id
            result.workflow_id = self.workflow_id

            # Update the run_response with the content from the result
            if result.content is not None and isinstance(result.content, str):
                self.run_response.content = result.content

            # Add the run to the memory
            self.memory.add_run(WorkflowRun(input=self.run_input, response=self.run_response))
            # Write this run to the database
            self.write_to_storage()
            log_debug(f"*********** Workflow Run End: {self.run_id} ***********")
            return result
        else:
            logger.warning(f"Workflow.run() should only return RunResponse objects, got: {type(result)}")
            return None

    def set_storage_mode(self):
        if self.storage is not None:
            if self.storage.mode in ["agent", "team"]:
                logger.warning(f"You shouldn't use storage in multiple modes. Current mode is {self.storage.mode}.")

            self.storage.mode = "workflow"

    def set_workflow_id(self) -> str:
        if self.workflow_id is None:
            self.workflow_id = str(uuid4())
        log_debug(f"*********** Workflow ID: {self.workflow_id} ***********")
        return self.workflow_id

    def set_session_id(self) -> str:
        if self.session_id is None:
            self.session_id = str(uuid4())
        log_debug(f"*********** Session ID: {self.session_id} ***********")
        return self.session_id

    def set_debug(self) -> None:
        if self.debug_mode or getenv("AGNO_DEBUG", "false").lower() == "true":
            self.debug_mode = True
            set_log_level_to_debug()
            log_debug("Debug logs enabled")
        else:
            set_log_level_to_info()

    def set_monitoring(self) -> None:
        if self.monitoring or getenv("AGNO_MONITOR", "false").lower() == "true":
            self.monitoring = True
        else:
            self.monitoring = False

        if self.telemetry or getenv("AGNO_TELEMETRY", "true").lower() == "true":
            self.telemetry = True
        else:
            self.telemetry = False

    def initialize_memory(self) -> None:
        if self.memory is None:
            self.memory = WorkflowMemory()

    def update_run_method(self):
        # Update the run() method to call run_workflow() instead of the subclass's run()
        # First, check if the subclass has a run method
        #   If the run() method has been overridden by the subclass,
        #   then self.__class__.run is not Workflow.run will be True
        if self.__class__.run is not Workflow.run:
            # Store the original run method bound to the instance in self._subclass_run
            self._subclass_run = self.__class__.run.__get__(self)
            # Get the parameters of the run method
            sig = inspect.signature(self.__class__.run)
            # Convert parameters to a serializable format
            self._run_parameters = {
                param_name: {
                    "name": param_name,
                    "default": param.default.default
                    if hasattr(param.default, "__class__") and param.default.__class__.__name__ == "FieldInfo"
                    else (param.default if param.default is not inspect.Parameter.empty else None),
                    "annotation": (
                        param.annotation.__name__
                        if hasattr(param.annotation, "__name__")
                        else (
                            str(param.annotation).replace("typing.Optional[", "").replace("]", "")
                            if "typing.Optional" in str(param.annotation)
                            else str(param.annotation)
                        )
                    )
                    if param.annotation is not inspect.Parameter.empty
                    else None,
                    "required": param.default is inspect.Parameter.empty,
                }
                for param_name, param in sig.parameters.items()
                if param_name != "self"
            }
            # Determine the return type of the run method
            return_annotation = sig.return_annotation
            self._run_return_type = (
                return_annotation.__name__
                if return_annotation is not inspect.Signature.empty and hasattr(return_annotation, "__name__")
                else str(return_annotation)
                if return_annotation is not inspect.Signature.empty
                else None
            )
            # Important: Replace the instance's run method with run_workflow
            # This is so we call run_workflow() instead of the subclass's run()
            object.__setattr__(self, "run", self.run_workflow.__get__(self))
        else:
            # If the subclass does not override the run method,
            # the Workflow.run() method will be called and will log an error
            self._subclass_run = self.run
            self._run_parameters = {}
            self._run_return_type = None

    def update_agent_session_ids(self):
        # Update the session_id for all Agent instances
        # use dataclasses.fields() to iterate through fields
        for f in fields(self):
            field_type = f.type
            if isinstance(field_type, Agent):
                field_value = getattr(self, f.name)
                field_value.session_id = self.session_id

    def get_workflow_data(self) -> Dict[str, Any]:
        workflow_data: Dict[str, Any] = {}
        if self.name is not None:
            workflow_data["name"] = self.name
        if self.workflow_id is not None:
            workflow_data["workflow_id"] = self.workflow_id
        if self.description is not None:
            workflow_data["description"] = self.description
        return workflow_data

    def get_session_data(self) -> Dict[str, Any]:
        session_data: Dict[str, Any] = {}
        if self.session_name is not None:
            session_data["session_name"] = self.session_name
        if self.session_state and len(self.session_state) > 0:
            session_data["session_state"] = nested_model_dump(self.session_state)
        if self.images is not None:
            session_data["images"] = [img.model_dump() for img in self.images]
        if self.videos is not None:
            session_data["videos"] = [vid.model_dump() for vid in self.videos]
        if self.audio is not None:
            session_data["audio"] = [aud.model_dump() for aud in self.audio]
        return session_data

    def get_workflow_session(self) -> WorkflowSession:
        """Get a WorkflowSession object, which can be saved to the database"""
        self.memory = cast(WorkflowMemory, self.memory)
        self.session_id = cast(str, self.session_id)
        self.workflow_id = cast(str, self.workflow_id)
        return WorkflowSession(
            session_id=self.session_id,
            workflow_id=self.workflow_id,
            user_id=self.user_id,
            memory=self.memory.to_dict() if self.memory is not None else None,
            workflow_data=self.get_workflow_data(),
            session_data=self.get_session_data(),
            extra_data=self.extra_data,
        )

    def load_workflow_session(self, session: WorkflowSession):
        """Load the existing Workflow from a WorkflowSession (from the database)"""

        # Get the workflow_id, user_id and session_id from the database
        if self.workflow_id is None and session.workflow_id is not None:
            self.workflow_id = session.workflow_id
        if self.user_id is None and session.user_id is not None:
            self.user_id = session.user_id
        if self.session_id is None and session.session_id is not None:
            self.session_id = session.session_id

        # Read workflow_data from the database
        if session.workflow_data is not None:
            # Get name from database and update the workflow name if not set
            if self.name is None and "name" in session.workflow_data:
                self.name = session.workflow_data.get("name")

        # Read session_data from the database
        if session.session_data is not None:
            # Get the session_name from database and update the current session_name if not set
            if self.session_name is None and "session_name" in session.session_data:
                self.session_name = session.session_data.get("session_name")

            # Get the session_state from database and update the current session_state
            if "session_state" in session.session_data:
                session_state_from_db = session.session_data.get("session_state")
                if (
                    session_state_from_db is not None
                    and isinstance(session_state_from_db, dict)
                    and len(session_state_from_db) > 0
                ):
                    # If the session_state is already set, merge the session_state from the database with the current session_state
                    if len(self.session_state) > 0:
                        # This updates session_state_from_db
                        merge_dictionaries(session_state_from_db, self.session_state)
                    # Update the current session_state
                    self.session_state = session_state_from_db

            # Get images, videos, and audios from the database
            if "images" in session.session_data:
                images_from_db = session.session_data.get("images")
                if images_from_db is not None and isinstance(images_from_db, list):
                    if self.images is None:
                        self.images = []
                    self.images.extend([ImageArtifact.model_validate(img) for img in images_from_db])
            if "videos" in session.session_data:
                videos_from_db = session.session_data.get("videos")
                if videos_from_db is not None and isinstance(videos_from_db, list):
                    if self.videos is None:
                        self.videos = []
                    self.videos.extend([VideoArtifact.model_validate(vid) for vid in videos_from_db])
            if "audio" in session.session_data:
                audio_from_db = session.session_data.get("audio")
                if audio_from_db is not None and isinstance(audio_from_db, list):
                    if self.audio is None:
                        self.audio = []
                    self.audio.extend([AudioArtifact.model_validate(aud) for aud in audio_from_db])

        # Read extra_data from the database
        if session.extra_data is not None:
            # If extra_data is set in the workflow, update the database extra_data with the workflow's extra_data
            if self.extra_data is not None:
                # Updates workflow_session.extra_data in place
                merge_dictionaries(session.extra_data, self.extra_data)
            # Update the current extra_data with the extra_data from the database which is updated in place
            self.extra_data = session.extra_data

        if session.memory is not None:
            if self.memory is None:
                self.memory = WorkflowMemory()

            try:
                if "runs" in session.memory:
                    try:
                        self.memory.runs = [WorkflowRun(**m) for m in session.memory["runs"]]
                    except Exception as e:
                        logger.warning(f"Failed to load runs from memory: {e}")
            except Exception as e:
                logger.warning(f"Failed to load WorkflowMemory: {e}")
        log_debug(f"-*- WorkflowSession loaded: {session.session_id}")

    def read_from_storage(self) -> Optional[WorkflowSession]:
        """Load the WorkflowSession from storage.

        Returns:
            Optional[WorkflowSession]: The loaded WorkflowSession or None if not found.
        """
        if self.storage is not None and self.session_id is not None:
            self.workflow_session = cast(WorkflowSession, self.storage.read(session_id=self.session_id))
            if self.workflow_session is not None:
                self.load_workflow_session(session=self.workflow_session)
        return self.workflow_session

    def write_to_storage(self) -> Optional[WorkflowSession]:
        """Save the WorkflowSession to storage

        Returns:
            Optional[WorkflowSession]: The saved WorkflowSession or None if not saved.
        """
        if self.storage is not None:
            self.workflow_session = cast(WorkflowSession, self.storage.upsert(session=self.get_workflow_session()))
        return self.workflow_session

    def load_session(self, force: bool = False) -> Optional[str]:
        """Load an existing session from the database and return the session_id.
        If a session does not exist, create a new session.

        - If a session exists in the database, load the session.
        - If a session does not exist in the database, create a new session.
        """
        # If a workflow_session is already loaded, return the session_id from the workflow_session
        #   if the session_id matches the session_id from the workflow_session
        if self.workflow_session is not None and not force:
            if self.session_id is not None and self.workflow_session.session_id == self.session_id:
                return self.workflow_session.session_id

        # Load an existing session or create a new session
        if self.storage is not None:
            # Load existing session if session_id is provided
            log_debug(f"Reading WorkflowSession: {self.session_id}")
            self.read_from_storage()

            # Create a new session if it does not exist
            if self.workflow_session is None:
                log_debug("-*- Creating new WorkflowSession")
                # write_to_storage() will create a new WorkflowSession
                # and populate self.workflow_session with the new session
                self.write_to_storage()
                if self.workflow_session is None:
                    raise Exception("Failed to create new WorkflowSession in storage")
                log_debug(f"-*- Created WorkflowSession: {self.workflow_session.session_id}")
                self.log_workflow_session()
        return self.session_id

    def new_session(self) -> None:
        """Create a new Workflow session

        - Clear the workflow_session
        - Create a new session_id
        - Load the new session
        """
        self.workflow_session = None
        self.session_id = str(uuid4())
        self.load_session(force=True)

    def log_workflow_session(self):
        log_debug(f"*********** Logging WorkflowSession: {self.session_id} ***********")

    def rename(self, name: str) -> None:
        """Rename the Workflow and save to storage"""

        # -*- Read from storage
        self.read_from_storage()
        # -*- Rename Workflow
        self.name = name
        # -*- Save to storage
        self.write_to_storage()
        # -*- Log Workflow session
        self.log_workflow_session()

    def rename_session(self, session_name: str):
        """Rename the current session and save to storage"""

        # -*- Read from storage
        self.read_from_storage()
        # -*- Rename session
        self.session_name = session_name
        # -*- Save to storage
        self.write_to_storage()
        # -*- Log Workflow session
        self.log_workflow_session()

    def delete_session(self, session_id: str):
        """Delete the current session and save to storage"""
        if self.storage is None:
            return
        # -*- Delete session
        self.storage.delete_session(session_id=session_id)

    def deep_copy(self, *, update: Optional[Dict[str, Any]] = None) -> Workflow:
        """Create and return a deep copy of this Workflow, optionally updating fields.

        Args:
            update (Optional[Dict[str, Any]]): Optional dictionary of fields for the new Workflow.

        Returns:
            Workflow: A new Workflow instance.
        """
        # Extract the fields to set for the new Workflow
        fields_for_new_workflow: Dict[str, Any] = {}

        for f in fields(self):
            field_value = getattr(self, f.name)
            if field_value is not None:
                if isinstance(field_value, Agent):
                    fields_for_new_workflow[f.name] = field_value.deep_copy()
                else:
                    fields_for_new_workflow[f.name] = self._deep_copy_field(f.name, field_value)

        # Update fields if provided
        if update:
            fields_for_new_workflow.update(update)

        # Create a new Workflow
        new_workflow = self.__class__(**fields_for_new_workflow)
        log_debug(f"Created new {self.__class__.__name__}")
        return new_workflow

    def _deep_copy_field(self, field_name: str, field_value: Any) -> Any:
        """Helper method to deep copy a field based on its type."""
        from copy import copy, deepcopy

        # For memory, use its deep_copy method
        if field_name == "memory":
            return field_value.deep_copy()

        # For compound types, attempt a deep copy
        if isinstance(field_value, (list, dict, set, Storage)):
            try:
                return deepcopy(field_value)
            except Exception as e:
                logger.warning(f"Failed to deepcopy field: {field_name} - {e}")
                try:
                    return copy(field_value)
                except Exception as e:
                    logger.warning(f"Failed to copy field: {field_name} - {e}")
                    return field_value

        # For pydantic models, attempt a model_copy
        if isinstance(field_value, BaseModel):
            try:
                return field_value.model_copy(deep=True)
            except Exception as e:
                logger.warning(f"Failed to deepcopy field: {field_name} - {e}")
                try:
                    return field_value.model_copy(deep=False)
                except Exception as e:
                    logger.warning(f"Failed to copy field: {field_name} - {e}")
                    return field_value

        # For other types, return as is
        return field_value
