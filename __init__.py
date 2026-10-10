"""Read one public profile and that author's recent posts, and watch one saved account."""
import json


def _json(payload: dict) -> str:
    # Hermes accepts a string tool result. A dict is rejected on v0.21.4 and on main.
    return json.dumps(payload, ensure_ascii=False)


def register(ctx) -> None:
    if __package__:
        from .cli import handle, setup_parser
        from .service import TOOLSET, author_feed, author_watch, profile_get
        from .service import Deps
    else:
        from cli import handle, setup_parser
        from service import TOOLSET, author_feed, author_watch, profile_get
        from service import Deps

    def _profile(args, **_kwargs):
        return _json(profile_get(Deps(), args or {}))

    def _feed(args, **_kwargs):
        return _json(author_feed(Deps(), args or {}))

    def _watch(args, **_kwargs):
        return _json(author_watch(Deps(), args or {}))

    ctx.register_tool(
        name="profile_get",
        toolset=TOOLSET,
        schema={
            "name": "profile_get",
            "description": (
                "Fetch one public profile by actor and return did, handle, displayName, "
                "followersCount, followsCount, and postsCount. "
                "avatar, banner, description, and pinnedPost are dropped. "
                "This tool does not write seen post URIs."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["actor"],
                "properties": {
                    "actor": {"type": "string", "description": "Handle or DID. Required."},
                },
            },
        },
        handler=_profile,
    )
    ctx.register_tool(
        name="author_feed",
        toolset=TOOLSET,
        schema={
            "name": "author_feed",
            "description": (
                "Fetch one page for actor with filter posts_no_replies. The request asks for 10 items. "
                "A page can be shorter than 10. "
                "Reposts are kind repost and keep the original author_handle. "
                "Pins are kind pin. This tool does not write seen post URIs and does not send includePins, "
                "a caller filter, or a caller limit."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["actor"],
                "properties": {
                    "actor": {"type": "string", "description": "Handle or DID. Required."},
                },
            },
        },
        handler=_feed,
    )
    ctx.register_tool(
        name="author_watch",
        toolset=TOOLSET,
        schema={
            "name": "author_watch",
            "description": (
                "Take no arguments. Seen post URIs advance only when this run is confirmed as the cron job "
                "and the fetch succeeds. If this run is not confirmed, including plugins.isolation: host, "
                "it cannot monitor, requests nothing, and leaves seen URIs unchanged, and it posts that notice on every run. "
                "The first confirmed run stores up to 3 pages and does not announce them. "
                "One run reads at most the latest 3 pages and announces a new post by this account on them. "
                "When every row on those pages is new, with no saved post or mark, "
                "and the feed still offers another cursor, it also posts one possible-miss notice. "
                "The rows it read are stored. When those rows are still on the pages the next run reads, "
                "that run overlaps and does not repeat the notice. When newer rows push them past "
                "the pages it reads, the same notice is quiet for 24 hours, and a new post it read is still announced. "
                "A run that finds a saved post or a stored mark does not post that notice. "
                "A post it announces from page 2 or later may be older than the first stored page."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        },
        handler=_watch,
    )
    ctx.register_cli_command(
        name="author-feed",
        help="Watch one public author: schedule, unschedule, or status",
        setup_fn=setup_parser,
        handler_fn=handle,
        description=(
            "Save one account and one cron job from this machine. "
            "Does not register a slash command."
        ),
    )
