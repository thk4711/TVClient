## Summary

<!-- What does this change and why? Link any related issue. -->

## Verification

<!-- Which checks did you run? Note hardware checks performed or still outstanding. -->

- [ ] Python tests (`python -m unittest discover -s tests`)
- [ ] `ruff check` and `mypy`
- [ ] Packaging (`python -m build` and `scripts/check_package.py`)
- [ ] Browser/native checks relevant to the change
- [ ] Tested on Raspberry Pi 4 hardware (describe below)

## Documentation

- [ ] Updated current behavior and limitations in the docs where applicable

## Checklist

- [ ] Change is focused and includes regression tests for changed behavior
- [ ] No credentials, device exports, private hostnames/addresses, logs,
      screenshots, browser profiles or build outputs are included
- [ ] New code is compatible with the project's MIT licence; third-party
      attribution and terms are preserved
