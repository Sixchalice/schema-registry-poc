"""Application factory. Run with: uvicorn --factory acl_api.main:create_app"""

import logging

from fastapi import FastAPI

from acl_api.api.error_handlers import install_error_handlers
from acl_api.api.routes import router
from acl_api.cli.sr_acl_cli import SrAclCli
from acl_api.services.acl_service import AclBackend, AclService
from acl_api.settings import Settings


def create_app(settings: Settings | None = None, backend: AclBackend | None = None) -> FastAPI:
    """Wires the app. Tests pass their own settings and a fake backend instead of the real CLI."""
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    backend = backend or SrAclCli(settings.cli_path, settings.cli_config, settings.cli_timeout)

    app = FastAPI(title="acl-api", version="2.0", description="REST API for sr-acl-cli")
    app.state.settings = settings
    app.state.acl_service = AclService(backend, settings.max_pending_writes)
    install_error_handlers(app)
    app.include_router(router)
    return app
