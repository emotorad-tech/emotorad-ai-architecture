"""Conversations and idempotency keys in one DynamoDB table.

Spec: docs/superpowers/specs/2026-09-28-dynamodb-conversation-store-design.md.

Low-level boto3 client, created lazily, so importing this module costs nothing
in offline mode. Every AWS failure becomes a typed error the runtime handles:
ConversationConflict when another server saved first, StoreUnavailable for
anything else. Nothing here ever continues on an empty state after a failed
read, which would silently lose the customer's conversation.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from ..contract import InboundMessage, Reply
from ..conversation import (
    ConversationConflict,
    ConversationState,
    ConversationSummaryItem,
    StoreUnavailable,
    TranscriptTurn,
    transcript_turns,
    utc_now_iso,
)

HOUR = 3600
DAY = 86400


def _s(value: Any) -> Dict[str, str]:
    return {"S": "" if value is None else str(value)}


def _n(value: float) -> Dict[str, str]:
    return {"N": str(int(value))}


def _is_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code") == "ConditionalCheckFailedException"


def _turn_starts(history: List[Dict[str, Any]]) -> List[int]:
    """Indexes where a customer turn begins: a user message whose content is
    text. Tool results are user messages too, but carry a list, and must never
    be cut from the tool call before them."""
    return [i for i, m in enumerate(history) if m.get("role") == "user" and isinstance(m.get("content"), str)]


class DynamoConversationStore:
    def __init__(
        self,
        table_name: str,
        client: Any = None,
        region: str = "ap-south-1",
        endpoint_url: Optional[str] = None,
        clock: Callable[[], str] = utc_now_iso,
        now: Callable[[], float] = time.time,
        state_ttl_hours: int = 48,
        transcript_ttl_days: int = 90,
        log: Any = None,
        max_state_bytes: int = 350_000,
    ) -> None:
        self.table = table_name
        self._client = client
        self._region = region
        self._endpoint = endpoint_url
        self._clock = clock
        self._now = now
        self._state_ttl = state_ttl_hours * HOUR
        self._transcript_ttl = transcript_ttl_days * DAY
        self._log = log
        self._max_state_bytes = max_state_bytes

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region, endpoint_url=self._endpoint)
        return self._client

    def _call(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return getattr(self.client, operation)(TableName=self.table, **kwargs)
        except Exception as exc:  # botocore raises ClientError, BotoCoreError and plain network errors
            if _is_conflict(exc):
                raise ConversationConflict("conversation was saved by another server") from None
            raise StoreUnavailable("DynamoDB %s failed (%s)" % (operation, type(exc).__name__)) from None

    # -- working state -----------------------------------------------------

    def get(self, conversation_id: str) -> ConversationState:
        response = self._call(
            "get_item", Key={"PK": _s("CONV#" + conversation_id), "SK": _s("STATE")}, ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return ConversationState(conversation_id=conversation_id, started_at=self._clock())
        state = ConversationState.from_json(item["state"]["S"])
        state.version = int(item["version"]["N"])
        return state

    def save(self, state: ConversationState) -> None:
        self._trim(state)
        expected = state.version
        item = {
            "PK": _s("CONV#" + state.conversation_id),
            "SK": _s("STATE"),
            "state": _s(state.to_json()),
            "version": _n(expected + 1),
            "user_key": _s(state.user_key),
            "updated_at": _s(self._clock()),
            "expires_at": _n(self._now() + self._state_ttl),
        }
        if expected == 0:
            self._call("put_item", Item=item, ConditionExpression="attribute_not_exists(PK)")
        else:
            self._call(
                "put_item", Item=item, ConditionExpression="version = :v",
                ExpressionAttributeValues={":v": _n(expected)},
            )
        state.version = expected + 1

    def _trim(self, state: ConversationState) -> None:
        """Keep the item under DynamoDB's 400 KB limit by dropping the oldest
        whole turns. The transcript is a separate item and keeps everything."""
        dropped = 0
        while len(state.to_json().encode("utf-8")) > self._max_state_bytes:
            starts = _turn_starts(state.history)
            if len(starts) < 2:
                break  # one turn left; the put will fail loudly rather than lose it
            del state.history[: starts[1]]
            dropped += 1
        if dropped and self._log is not None:
            self._log.emit("history_trimmed", state.conversation_id, turns_dropped=dropped)

    # -- transcript and summaries -----------------------------------------

    def record_turn(
        self,
        state: ConversationState,
        inbound: InboundMessage,
        reply: Reply,
        summary: Optional[ConversationSummaryItem] = None,
    ) -> None:
        expires = _n(self._now() + self._transcript_ttl)
        for turn in transcript_turns(state, inbound, reply, self._clock()):
            self._call("put_item", Item={
                "PK": _s("CONV#" + state.conversation_id), "SK": _s("TURN#%05d" % turn.n),
                "turn": _s(json.dumps(asdict(turn), ensure_ascii=False)), "expires_at": expires,
            })
        if summary is not None and state.user_key:
            self._call("put_item", Item={
                "PK": _s("USER#" + state.user_key),
                "SK": _s("CONV#%s#%s" % (summary.started_at, summary.conversation_id)),
                "summary": _s(json.dumps(asdict(summary), ensure_ascii=False)), "expires_at": expires,
            })

    def _query_all(self, **kwargs: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        while True:
            response = self._call("query", **kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response or kwargs.get("Limit"):
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def transcript(self, conversation_id: str) -> List[TranscriptTurn]:
        items = self._query_all(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :turn)",
            ExpressionAttributeValues={":pk": _s("CONV#" + conversation_id), ":turn": _s("TURN#")},
        )
        turns = []
        for item in items:
            raw = json.loads(item["turn"]["S"])
            raw["attachments"] = tuple(raw.get("attachments") or ())
            turns.append(TranscriptTurn(**raw))
        return sorted(turns, key=lambda t: t.n)

    def recent_summaries(
        self, user_key: str, limit: int = 3, exclude: Optional[str] = None
    ) -> List[ConversationSummaryItem]:
        items = self._query_all(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :conv)",
            ExpressionAttributeValues={":pk": _s("USER#" + user_key), ":conv": _s("CONV#")},
            ScanIndexForward=False, Limit=limit + 1,
        )
        summaries = [ConversationSummaryItem(**json.loads(item["summary"]["S"])) for item in items]
        return [s for s in summaries if s.conversation_id != exclude][:limit]

    def history(self, conversation_id: str) -> List[Dict[str, Any]]:
        return self.get(conversation_id).history
