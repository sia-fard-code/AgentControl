"""Pure execution-profile selection; profiles grant no approval authority."""
import json

PROFILES = ("legacy", "resilient-v1")
MAX_SETTINGS_BYTES = 4096


def _validate_settings(settings):
    if (type(settings) is not dict or set(settings) != {"schema", "default_profile"}
            or type(settings["schema"]) is not int or settings["schema"] != 1
            or settings["default_profile"] not in PROFILES):
        raise ValueError("invalid_settings")
    return settings


def parse_settings(raw):
    """Decode only the closed operator metadata schema, without filesystem I/O."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid_settings")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("invalid_settings")

    try:
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ValueError("invalid_settings")
        return _validate_settings(json.loads(raw.decode("utf-8"),
                                            object_pairs_hook=pairs,
                                            parse_constant=nonfinite))
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise ValueError("invalid_settings") from None


def resolve_profile(profile=None, *, oneshot=False, supervisor_manifest=False,
                    approved_worker=False, settings=None):
    """Resolve metadata only; None means omitted profile or absent settings."""
    if profile is not None:
        if profile not in PROFILES:
            raise ValueError("invalid_profile")
        selected, provenance = profile, "explicit"
    elif oneshot:
        selected, provenance = "legacy", "oneshot"
    elif supervisor_manifest:
        selected, provenance = "legacy", "supervisor-manifest"
    elif approved_worker:
        selected, provenance = "legacy", "approved-worker"
    elif settings is not None:
        selected = _validate_settings(settings)["default_profile"]
        provenance = "operator-settings"
    else:
        selected, provenance = "legacy", "legacy-default"
    return {"profile": selected, "provenance": provenance}
