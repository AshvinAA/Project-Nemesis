"""Project Nemesis — Python RL agent for the BuddyDoom Shotgun Guy.

Speaks the engine's existing TCP :31666 director protocol (observe / act /
spawn). The Q-learning phase builds on this glue layer; Phase 1 is deliberately
learning-free (hardcoded policy) to prove the loop end-to-end first.
"""

__version__ = "0.1.0"
