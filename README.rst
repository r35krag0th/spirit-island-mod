Spirit Island - [By MJ & iakona]
--------------------------------

This is a mod for Tabletop Simulator of Spirit Island. This copy is an unpacked version using `tabletop-tools <https://pypi.org/project/tabletop-tools/>`_.

To build the mod::

    $ tts repack

This will put the module at ``build/packed-savegame.json``. You can also pass a path for where to build the module.

You can also use::

    $ tts unpack <...path/to/savegame.json...>

to unpack a savegame.

or::

    $ tts unpack --fileid 2104364163

to unpack from workshop.

Live debugging against a running TTS
------------------------------------

``tools/tts_dev.py`` speaks the TTS External Editor API (TTS listens on
``127.0.0.1:39999``, replies to ``127.0.0.1:39998``) with no dependencies
beyond the Python standard library. Only one program can hold port 39998,
so close the VS Code TTS extension (or don't run ``listen``) when using it::

    $ uv run --no-project python tools/tts_dev.py listen          # terminal 1, keep open
    $ uv run --no-project python tools/tts_dev.py exec --wait 'return JSON.encode({n = #getAllObjects()})'
    $ uv run --no-project python tools/tts_dev.py exec -f probe.lua --guid 089896
    $ uv run --no-project python tools/tts_dev.py diff --show      # live scripts vs repo
    $ uv run --no-project python tools/tts_dev.py push --yes       # Save & Play with repo scripts
    $ uv run --no-project python tools/tts_dev.py repack --install # build and drop into TTS Saves

``listen`` logs every ``print``, script error, return value and script dump
to ``build/tts-messages.jsonl``; ``tail`` shows the recent ones. Returning a
Lua table from ``exec`` yields no reply; return ``JSON.encode(t)`` instead.
``push`` only replaces scripts on Global and top-level objects; anything
nested in a bag needs ``repack --install`` and a reload of that save.
