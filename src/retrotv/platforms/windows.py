from retrotv.platforms.base import PlatformAdapter


class WindowsAdapter(PlatformAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="windows",
            player_ipc_kind="windows",
            service_kind="foreground",
        )

