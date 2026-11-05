import httpx
import pytest

from gilgamosh.llm import provider_error


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (400, {"message": "z-ai/glm-flash-latest is not a valid model ID"}, "Ungültige Modellkennung"),
        (401, {"message": "Invalid credentials"}, "API-Key"),
        (402, {"message": "Insufficient credits"}, "Guthaben"),
        (403, {"message": "Forbidden"}, "Berechtigungen"),
        (400, {"message": "Invalid response_format schema"}, "JSON-Ausgabe"),
        (400, {"message": "maximum context length exceeded"}, "Kontextlimit"),
        (404, {"message": "Not found"}, "nicht gefunden"),
    ],
)
def test_provider_error_is_actionable_without_exposing_raw_content(status, error, expected, caplog):
    error["sensitive_provider_details"] = "PRIVATE_PROMPT_AND_KEY"
    result = provider_error(httpx.Response(status, json={"error": error}))
    assert expected in str(result)
    assert "PRIVATE_PROMPT_AND_KEY" not in str(result)
    assert "PRIVATE_PROMPT_AND_KEY" not in caplog.text


def test_non_json_provider_error_is_safe():
    result = provider_error(httpx.Response(400, text="<html>PRIVATE_PROVIDER_RESPONSE</html>"))
    assert str(result) == "LLM-Anfrage abgelehnt. Verbindung, Modell und Modellfähigkeiten prüfen."
