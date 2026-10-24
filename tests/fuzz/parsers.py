"""Coverage-guided fuzzing of the parsers that read outside input.

Atheris grows the bytes and Hypothesis's fuzz_one_input turns them into the
text of the property the suite already runs (``every_parser_answers_any_text``
in ``hq/platform/application/tests/test_properties.py``):

    DJANGO_SETTINGS_MODULE=hq.config.settings DJANGO_SECRET_KEY=fuzz DJANGO_DEBUG=1 \
    SEVERINO_DATABASE_PATH=var/fuzz.sqlite3 uv run --with atheris -m tests.fuzz.parsers -max_total_time=300
"""

import sys

if __name__ == "__main__":
    import atheris
    import django

    with atheris.instrument_imports():
        django.setup()
        from hq.platform.application.tests.test_properties import every_parser_answers_any_text

    atheris.Setup(sys.argv, every_parser_answers_any_text.hypothesis.fuzz_one_input)
    atheris.Fuzz()
