"""Keep pytest out of the fixture repos.

The repos under ``workspaces/`` hold their own tests, and some fail on purpose
(a seeded bug the agent is asked to fix). They are copied into a scratch
workspace and run there, never collected from here.
"""

collect_ignore_glob = ["workspaces/*"]
