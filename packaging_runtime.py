"""Initialize site paths required by Kivy dependency packages in frozen apps."""
import site
import sys

if site.USER_BASE is None:
    site.USER_BASE = sys.prefix
