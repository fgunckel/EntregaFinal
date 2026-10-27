"""Base común de contratos: modelo estricto que rechaza campos extra."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base de todos los contratos: rechaza campos no declarados (patrón M7)."""

    model_config = ConfigDict(extra="forbid")
