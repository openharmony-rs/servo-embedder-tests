from .desktop import DesktopHost
from .ohos import HilogStream, OhosDevice

Platform = OhosDevice | DesktopHost

__all__ = ["DesktopHost", "HilogStream", "OhosDevice", "Platform"]
