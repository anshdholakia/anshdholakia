"""kg-agent-supervisor.

A supervisor that walks a knowledge graph of tasks, prompts a Gemini-backed
Google Chat bot for each task, watches the replies, and automatically recovers
(restart + replay context + resume) when the agent hangs or dies.
"""

__version__ = "0.1.0"
