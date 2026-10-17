#!/usr/bin/env python3
"""Opt-in AI provider smoke check.

Uses the configured AI adapter (simulated by default, or an OpenAI-compatible
provider via FLOWOPS_AI_*). It never prints the API key and is not part of CI.

    python scripts/ai_smoke.py --title "VPN outage - all users cannot work"
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ai_agent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="VPN outage - all users cannot work")
    parser.add_argument("--queue", default="it-service-desk")
    parser.add_argument("--priority", default="normal")
    parser.add_argument("--ticket-id", default="ai-smoke-1")
    args = parser.parse_args()

    adapter = ai_agent.adapter_from_environment()
    snapshot = ai_agent.TicketSnapshot(
        ticket_id=args.ticket_id,
        requester_id=7,
        title=args.title,
        status="open",
        priority=args.priority,
        category=None,
        queue=args.queue,
    )
    try:
        result = adapter.analyze(snapshot, history=[])
    except ai_agent.AIAvailabilityError as error:
        print(f"AI provider unavailable: {error}")
        return 1

    print(f"adapter: {result.adapter}")
    print(f"summary: {result.summary.text}")
    print(
        "knowledge_base: "
        + json.dumps(
            [
                {"id": match.article.id, "score": match.score}
                for match in result.knowledge_base
            ]
        )
    )
    print(
        "recommendations: "
        + json.dumps(
            [
                {
                    "id": recommendation.id,
                    "kind": recommendation.kind,
                    "suggested": recommendation.suggested,
                    "applicable": recommendation.applicable,
                    "confidence": recommendation.confidence,
                }
                for recommendation in result.recommendations
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
