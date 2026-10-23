"""Documented environment settings are parsed explicitly, with secret-safe errors."""
import pytest

from .config import AppSettings, load_settings


def test_empty_environment_preserves_development_defaults():
    assert load_settings({}).model_dump() == AppSettings().model_dump()


def test_environment_tokens_and_routing_url_are_loaded_without_global_mutation():
    result = load_settings({"CLEANROUTEAI_ENVIRONMENT": "production", "CLEANROUTEAI_OPERATOR_TOKEN": "operator-example",
        "CLEANROUTEAI_SUPERVISOR_TOKEN": "supervisor-example", "CLEANROUTEAI_ADMIN_TOKEN": "admin-example",
        "CLEANROUTEAI_INTERNAL_SERVICE_TOKEN": "service-example", "CLEANROUTEAI_VALHALLA_URL": "https://routing.example.test/api"})
    assert result.environment == "production"
    assert result.operator_token == "operator-example"
    assert result.supervisor_token == "supervisor-example"
    assert result.admin_token == "admin-example"
    assert result.internal_service_token == "service-example"
    assert result.valhalla_url == "https://routing.example.test/api"
    assert result.enable_hardware_ros2_adapter is False


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ("0", False),
    ("1", True), (" OFF ", False), ("YES", True)])
def test_boolean_settings_do_not_treat_false_string_as_true(value, expected):
    loaded = load_settings({"CLEANROUTEAI_ALLOW_LABELED_DEMO_ROUTING": value,
        "CLEANROUTEAI_ENABLE_DEFAULT_CAMPUS_TERRAIN_LAYER": value})
    assert loaded.allow_labeled_demo_routing is expected
    assert loaded.enable_default_campus_terrain_layer is expected


def test_invalid_boolean_reports_only_its_variable_name():
    secret = "credential-value-that-must-not-be-printed"
    with pytest.raises(ValueError) as error:
        load_settings({"CLEANROUTEAI_ALLOW_LABELED_DEMO_ROUTING": secret, "CLEANROUTEAI_OPERATOR_TOKEN": secret})
    assert "CLEANROUTEAI_ALLOW_LABELED_DEMO_ROUTING" in str(error.value)
    assert secret not in str(error.value)


@pytest.mark.parametrize("value", ["ftp://routing.test", "http://", "https://user:secret@routing.test", "http://routing.test:invalid"])
def test_routing_url_requires_http_and_does_not_echo_invalid_value(value):
    with pytest.raises(ValueError) as error:
        load_settings({"CLEANROUTEAI_VALHALLA_URL": value})
    assert "CLEANROUTEAI_VALHALLA_URL" in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize("value,expected", [
    ('["https://console.example.test", "http://localhost:5173"]', ["https://console.example.test", "http://localhost:5173"]),
    ("https://one.test, http://localhost:5174", ["https://one.test", "http://localhost:5174"]),
    ("[]", []), ("*", ["*"]),
])
def test_cors_accepts_json_lists_or_comma_separated_origins(value, expected):
    assert load_settings({"CLEANROUTEAI_CORS_ORIGINS": value}).cors_origins == expected


@pytest.mark.parametrize("value", ['["https://one.test", 4]', "[invalid", "javascript:invalid", "https://one.test/path"])
def test_cors_rejects_invalid_origins(value):
    with pytest.raises(ValueError, match="CLEANROUTEAI_CORS_ORIGINS"):
        load_settings({"CLEANROUTEAI_CORS_ORIGINS": value})


def test_blank_tokens_disable_the_configured_token_and_hardware_env_is_ignored():
    settings = load_settings({"CLEANROUTEAI_OPERATOR_TOKEN": "", "CLEANROUTEAI_ADMIN_TOKEN": "  ",
        "CLEANROUTEAI_ENABLE_HARDWARE_ROS2_ADAPTER": "true"})
    assert settings.operator_token is None
    assert settings.admin_token is None
    assert settings.enable_hardware_ros2_adapter is False
