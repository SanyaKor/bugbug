"""A click on a button that offers to start an agent run."""

import logging
from typing import Any
from uuid import UUID

from hackbot_client import HackbotClient
from pydantic import AliasChoices, BaseModel, Field, Json, ConfigDict, validate_call
from slack_bolt.context.ack.async_ack import AsyncAck
from slack_bolt.context.async_context import AsyncBoltContext
from slack_bolt.context.respond.async_respond import AsyncRespond

from app.config import settings

log = logging.getLogger(__name__)


class StartAgentRunValue(BaseModel):
    """What a button that starts an agent run carries."""

    agent_name: str
    # A message keeps its buttons clickable, so the values posted
    # before the key was renamed from `params` are still out there.
    # FIXME: This alias can be removed after a few months, when we know
    # all messages are old enough. Target date: 2027-02-01.
    inputs: dict[str, Any] = Field(
        default={}, validation_alias=AliasChoices("inputs", "params")
    )
    dedupe_key: str = Field(min_length=1)
    # A run whose pending actions will be applied before the new run starts.
    apply_run_id: UUID | None = None

class SlackUser(BaseModel):
    """User who started a Slack interaction.
    """
    id: str | None = None

class SlackClickPayload(BaseModel):
    """The Slack payload received when a button is clicked.
        https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/
    """
    user: SlackUser | None = None

class SlackClickAction(BaseModel):
    """The action that starts an agent run.
        https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/
    """
    value: Json[StartAgentRunValue]

def _run_url(run_id: UUID) -> str:
    return f"{settings.hackbot_ui_url}/runs/{run_id}"

@validate_call(config=ConfigDict(arbitrary_types_allowed=True))
async def start_agent_run_callback(
    ack: AsyncAck,
    action: SlackClickAction,
    body: SlackClickPayload,
    context: AsyncBoltContext,
    respond: AsyncRespond,
    logger: logging.Logger,
) -> None:
    """Start an agent run based on a Slack click.

    FIXME: we trigger the run before we acknowledge, which has a short timeout
    of 3 seconds. We should queue the run and acknowledge immediately, this
    should be fixed with https://github.com/mozilla/bugbug/issues/6468.
    """
    client: HackbotClient = context["hackbot_client"]
    user = body.user.id if body.user is not None else None
    value = action.value

    if value.apply_run_id:
        actions = await client.apply_actions(value.apply_run_id)
        if not actions.all_applied:
            logger.error(
                "Slack click by '%s' failed to apply run %s's actions: %s",
                user,
                value.apply_run_id,
                actions.failure_summary,
            )
            await respond(
                text=(
                    ":warning: Could not apply the actions of the run that was "
                    "supposed to be approved by this click. No new agent run was started."
                ),
                response_type="ephemeral",
                replace_original=False,
            )
            await ack()
            return

    run = await client.trigger_run(
        value.agent_name, value.inputs, dedupe_key=value.dedupe_key
    )

    if not run.is_new:
        logger.warning(
            "Slack click by '%s' for key '%r' ignored because a run with the same key run already exists (id=%s)",
            user,
            value.dedupe_key,
            run.run_id,
        )
        await respond(
            text=(
                f"A {value.agent_name} run is already triggered for this "
                f"({run.status.value}): {_run_url(run.run_id)}"
            ),
            response_type="ephemeral",
            replace_original=False,
            unfurl_links=False,
        )

    await ack()
