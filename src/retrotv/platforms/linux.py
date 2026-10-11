from retrotv.platforms.base import PlatformAdapter


class LinuxAdapter(PlatformAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="linux",
            player_ipc_kind="unix",
            service_kind="systemd",
        )

