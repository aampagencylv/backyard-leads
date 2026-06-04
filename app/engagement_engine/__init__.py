"""LeadProspector Engagement Engine.

Continuous lead nurture engine that pursues a contact until they become a
customer or tell us no — possibly for 12+ months — using AI to react to
real-world signals about the prospect.

Design reference: docs/ENGAGEMENT_ENGINE_DESIGN.md v3
Phase 1 (this module) ships:
  - Protocol interfaces (LLMProvider, ActionDispatcher, SignalSource)
  - Pydantic schemas for AI decision outputs
  - validate_ai_action output classifier (the prompt-injection defense layer)

No production worker reads or writes the engagement_* tables yet. Phase 2
brings the dispatcher; Phase 3 the signal watcher; Phase 4 the decision
maker.
"""
__version__ = "0.1.0-phase1"
