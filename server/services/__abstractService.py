from pydantic import BaseModel, Field, PrivateAttr, field_validator
import inspect
import os
from abc import ABC, abstractmethod
from typing import Optional


def get_initializer_filename() -> Optional[str]:
    # Walk through the stack to find where this class was instantiated
    current_file = os.path.abspath(__file__)
    for frame_info in inspect.stack():
        frame = frame_info.frame
        filename = os.path.abspath(frame_info.filename)
        if filename != current_file:
            return os.path.splitext(
                os.path.basename(filename)
            )[0]  # Return the filename of the first frame that isn't this file
    return None


class Path(BaseModel):
    prod: str | None
    dev: str | None

    @staticmethod
    def __sharedValidation(v: str | None) -> str | None:
        if v == None:
            return v
        if not isinstance(v, str):
            raise ValueError("Path must be a string")
        v = v.rstrip("/") + "/"
        if not v.startswith("http"):
            raise ValueError("Path must be a valid URL starting with http:// or https://")
        return v

    @field_validator("prod", mode="before")
    @classmethod
    def validate_prod(cls, v: str | None) -> str | None:
        return cls.__sharedValidation(v)

    @field_validator("dev", mode="before")
    @classmethod
    def validate_dev(cls, v: str | None) -> str | None:
        return cls.__sharedValidation(v)


class AbstractService(BaseModel, ABC):
    path: Path
    name: Optional[str] = None
    projectName: Optional[str] = Field(
        default=None,
        description=(
            "The target repo's AppConfig().projectName. Set it whenever it differs from the "
            "filename-derived `name` (e.g. file accountingService.py -> projectName='accounting'). "
            "Mock Pub/Sub messages are addressed with this value; `name` still drives the "
            "PUBSUB_ENABLED_* env vars and *_PROD_URL validation."))
    cloudRunServiceName: str
    localRepoDirName: str = Field(
        min_length=1,
        description=(
            "The target repo's checkout folder name under the shared GitHub parent directory "
            "(e.g. 'accountingSystem'). AppCreator.py reads ../<localRepoDirName>/server/AppCreator.py "
            "PORT and refuses to generate when it differs from the port in `path.dev`: a wrong dev "
            "port makes every PUBSUB_ENABLED-gated .request() call fail with a connection error, or "
            "reach whatever unrelated app is bound on that port."))
    # isOnline's ping result, None until something first reads isOnline.
    _isOnline: Optional[bool] = PrivateAttr(default=None)

    def __init__(self, **data):
        # Set name only if not explicitly provided
        if 'name' not in data or data['name'] is None:
            data['name'] = get_initializer_filename()
        super().__init__(**data)

        # Guard: filename must match class name (camelCase vs PascalCase)
        class_name = self.__class__.__name__
        expected_filename = class_name[0].lower() + class_name[1:]
        if self.name != expected_filename:
            raise ValueError(
                f"Service filename '{self.name}' does not match class name '{class_name}'. "
                f"Expected filename: '{expected_filename}'"
            )

    @property
    def isOnline(self) -> bool:
        """Whether the service answered a ping. Pinged on the first read, not
        when the service object is built: building one happens at import, on
        every worker start, and a ping there would wake an idle sibling."""
        if self._isOnline is None:
            self._isOnline = self.ping()
        return self._isOnline

    def getProjectName(self) -> str:
        """Consumer identity used to address Pub/Sub mock messages."""
        return self.projectName or self.name

    def isPubSubEnabled(self) -> bool:
        from AppConfig import AppConfig
        return AppConfig().isServiceEnabled(self.name)

    def _validateProdUrl(self) -> None:
        import re
        upper_snake = re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', self.name).upper()
        pubsub_enabled = os.getenv('PUBSUB_ENABLED_' + upper_snake, '').lower() == 'true'
        if self.path.prod is None and pubsub_enabled:
            raise ValueError(
                f"{upper_snake}_PROD_URL environment variable is not set for {self.name}. "
                f"This is required when PubSub is enabled for this service."
            )

    @abstractmethod
    def request(self):
        pass

    @abstractmethod
    def ping(self) -> bool:
        """Ping the service to check if it's available."""
        pass
