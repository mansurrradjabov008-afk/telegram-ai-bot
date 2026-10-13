"""
Pydantic Compatibility Fallback Layer.
Provides pure-Python shims for BaseModel, Field, BaseSettings, and SettingsConfigDict.
Ensures Antigravity Butler runs on mobile platforms (Android/Termux) without Rust compilation.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from pydantic import BaseModel as _PydanticBaseModel, Field as _PydanticField
    from pydantic_settings import BaseSettings as _PydanticBaseSettings, SettingsConfigDict as _PydanticSettingsConfigDict
    
    BaseModel = _PydanticBaseModel
    Field = _PydanticField
    BaseSettings = _PydanticBaseSettings
    SettingsConfigDict = _PydanticSettingsConfigDict
    HAS_PYDANTIC = True

except ImportError:
    HAS_PYDANTIC = False

    def Field(*args, default=None, default_factory=None, description=None, **kwargs):
        if default_factory is not None:
            return default_factory()
        return default

    class BaseModel:
        """Pure-Python fallback for Pydantic BaseModel."""
        def __init__(self, **kwargs):
            # Assign class defaults first
            for k in dir(self.__class__):
                if not k.startswith("_") and not callable(getattr(self.__class__, k)):
                    setattr(self, k, getattr(self.__class__, k))
            # Override with kwargs
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self) -> Dict[str, Any]:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

        def dict(self) -> Dict[str, Any]:
            return self.model_dump()

        def __repr__(self) -> str:
            attrs = ", ".join(f"{k}={v!r}" for k, v in self.model_dump().items())
            return f"{self.__class__.__name__}({attrs})"

    def SettingsConfigDict(**kwargs):
        return kwargs

    class BaseSettings:
        """Pure-Python fallback for Pydantic BaseSettings."""
        def __init__(self, **kwargs):
            # 1. Populate all class-level attributes
            for attr in dir(self.__class__):
                if not attr.startswith("_") and not callable(getattr(self.__class__, attr)):
                    setattr(self, attr, getattr(self.__class__, attr))

            # 2. Read from .env if present
            env_file = getattr(self, "model_config", {}).get("env_file", ".env") if hasattr(self, "model_config") else ".env"
            env_vars: Dict[str, Any] = {}
            if isinstance(env_file, str) and Path(env_file).exists():
                try:
                    from dotenv import dotenv_values
                    env_vars = dotenv_values(env_file)
                except ImportError:
                    pass

            # 3. Merge .env + os.environ + kwargs
            combined = {**env_vars, **os.environ, **kwargs}
            for k, v in combined.items():
                if hasattr(self, k):
                    curr = getattr(self, k)
                    if isinstance(curr, bool):
                        setattr(self, k, str(v).lower() in ("true", "1", "yes", "on"))
                    elif isinstance(curr, int) and not isinstance(curr, bool):
                        try:
                            setattr(self, k, int(v))
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(curr, float):
                        try:
                            setattr(self, k, float(v))
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(curr, list) and isinstance(v, str):
                        # comma or newline separated
                        items = [i.strip() for i in v.split(",") if i.strip()]
                        setattr(self, k, items)
                    else:
                        setattr(self, k, v)
