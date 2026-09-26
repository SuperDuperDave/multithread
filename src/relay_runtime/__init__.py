"""Account-installed Multithread execution and enrollment boundary."""


def account_launcher(*, compatibility=False):
    """Name the account entry; selection is not proof of installation or trust."""
    from pathlib import Path
    import os
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin" / (
        "relay" if compatibility else "multithread")


def hook_argv(launcher, client, repo):
    """The reviewed provider-hook command for one client and checkout.

    Codex trusts a hook by event slot and exact command text, never by
    checkout, so its command names none: one review then covers every enrolled
    checkout. The hook takes enrollment from Codex's session working directory,
    which launch and peer set to the checkout. Claude keeps no per-hook trust
    record, so its command keeps the explicit checkout.
    """
    selector = [] if client == "codex" else ["--repo", str(repo)]
    return [str(launcher), *selector, "provider-hook", "--client", client]
