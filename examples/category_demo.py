"""
Category architecture demo: from a platform task to routed worker + budget
ceiling + permission tier + connector needs. No keys, no funds.

  python examples/category_demo.py
"""

from __future__ import annotations

from sovereign_os.agents.auth import SovereignAuth
from sovereign_os.agents.categories import categorize
from sovereign_os.connectors import readiness_for_category
from sovereign_os.governance.budget_policy import CategoryBudgetPolicy

# Sample tasks as the marketplaces emit them (platform label, text).
TASKS = [
    ("Bug Fix", "Render a coherent image on an E-ink display from the Quartz64A"),
    ("", "Write a 600-word blog post about AI agents"),
    ("", "Research the BNPL competitive landscape, top 5 players"),
    ("design", "Design a clean settings page with dark mode"),
    ("", "Send a 3-email cold outreach sequence to D2C founders"),
    ("data", "Analyze this churn CSV and find the top drivers"),
    ("automation", "Automate posting our changelog to Slack on release"),
]


def main() -> None:
    policy = CategoryBudgetPolicy()
    print(f"{'PLATFORM TASK':<42} {'CATEGORY':<10} {'SKILL':<15} {'RISK':<7} {'BUDGET':<7} {'CAP':<16} CONNECTORS")
    print("-" * 130)
    for label, text in TASKS:
        cat = categorize(label, text)
        ceiling = policy.ceiling_cents_for_category(cat) / 100
        ready = readiness_for_category(cat)
        conns = ", ".join(f"{n}{'✓' if ok else '·'}" for n, ok in ready.items()) or "—"
        print(f"{(text[:40]):<42} {cat.key:<10} {cat.skill:<15} {cat.risk:<7} "
              f"${ceiling:<6.2f} {cat.capability.value:<16} {conns}")

    print("\n--- Permission is earned per category ---")
    auth = SovereignAuth()
    agent = "worker-7"
    for _ in range(8):
        auth.record_audit(agent, passed=True, score=0.9, category="writing")
    print(f"  After 8 writing wins: writing trust={auth.category_trust(agent, 'writing')}, "
          f"coding trust={auth.category_trust(agent, 'coding')}")
    print(f"  Can WRITE_FILES for writing? {auth.check_permission_for(agent, _cap('WRITE_FILES'), 'writing')}")
    print(f"  Can WRITE_FILES for coding?  {auth.check_permission_for(agent, _cap('WRITE_FILES'), 'coding')}")
    print(f"  Autonomous spend ceiling — writing: ${auth.max_spend_cents_for(agent, 'writing')/100:.2f} "
          f"vs coding: ${auth.max_spend_cents_for(agent, 'coding')/100:.2f}")


def _cap(name):
    from sovereign_os.agents.auth import Capability
    return Capability[name]


if __name__ == "__main__":
    main()
