# Fixture: does NOT violate Invariant 2. "socketserver" merely shares a prefix
# with "socket"; a substring check would produce a false positive here.
import socketserver

from dataclasses import dataclass

__all__ = ["socketserver", "dataclass"]
