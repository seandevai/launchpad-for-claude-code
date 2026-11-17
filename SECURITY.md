# Security policy

Launchpad starts local programs (`claude`, a terminal, Claude Desktop) and reads local files, so security reports matter.

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Report it privately through GitHub's **Security → Report a vulnerability** form on this repository.

Please include the steps to reproduce it and the impact you expect.

## Design notes

- The UI talks to Python only through the methods of the `Api` class in `launchpad.py`. Internal state is kept in underscore attributes, which pywebview does not expose to JavaScript.
- Values that come from the file system, such as folder names and session titles, are always escaped before they reach the HTML. Values placed inside inline handlers go through `jsq()`, which encodes every character that could break out of the string.
- Commands are started with argument lists, never with string concatenation. Session IDs must be UUIDs, and the mode must be `terminal` or `desktop`.
- `ccusage` is pinned to an exact version and called with fixed arguments.
- State and logs live in `~/.launchpad-cc/`, outside the folder that pywebview serves to the UI.
