from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import config
from server.review.harness import (
    HarnessProfile,
    create_harness,
    list_harnesses,
    set_vendor_prompt,
    validate_harness_name,
)


router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("")
def list_harness():
    return {"harnesses": list_harnesses()}


def _valid_name_or_400(name: str) -> None:
    try:
        validate_harness_name(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid harness name")


@router.get("/{name}")
def get_harness(name: str):
    _valid_name_or_400(name)
    if not (config.HARNESS_DIR / name).is_dir():
        raise HTTPException(status_code=404, detail="harness not found")
    hp = HarnessProfile.load(name)
    return {
        "name": hp.name,
        "system_prompt": hp.system_prompt,
        "vendor_prompts": hp.vendor_prompts,
        "claude_allowed_tools": hp.claude_allowed_tools,
        "codex_sandbox": hp.codex_sandbox,
    }


class HarnessPut(BaseModel):
    system_prompt: str | None = None
    vendor_prompts: dict[str, str] | None = None


@router.put("/{name}")
def put_harness(name: str, body: HarnessPut):
    _valid_name_or_400(name)
    base = config.HARNESS_DIR / name
    if not base.is_dir():
        create_harness(name, system_prompt=body.system_prompt)
    elif body.system_prompt is not None:
        (base / "review-system-prompt.md").write_text(body.system_prompt)
    if body.vendor_prompts is not None:
        try:
            for vendor, text in body.vendor_prompts.items():
                set_vendor_prompt(name, vendor, text)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid vendor")
    return get_harness(name)
