from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(Output):
    frage: str
    kontext: str


class Position(Output):
    agent: str
    runde: int
    position: str
    begruendung: str
    tragbar: bool | None
    zugestaendnisse: list[str]


class Coalition(Output):
    agent: str
    aktion: Literal["bleiben", "beitreten", "gruenden", "austreten"]
    ziel_gruppe: str | None
    partner: list[str]
    begruendung: str


class Consent(Output):
    zustimmung: bool


class GroupMessage(Output):
    agent: str
    gruppe: str
    nachricht_nr: int
    inhalt: str


class Statement(Output):
    agent: str
    gruppe: str
    statement: str
    einigungen_in_gruppe: list[str]
    zugestaendnisse: list[str]


class Vote(Output):
    agent: str
    stimme: Literal["zustimmen", "tolerieren", "ablehnen"]
    begruendung: str
    verletzte_rote_linie: str | None


class Review(Output):
    agent: str
    versuch: int
    bewertung: Literal[0, 1, 2]
    entscheidung: Literal["freigegeben", "zurueckgewiesen", "enthaltung"]
    verletzte_rote_linie: str | None
    hinweis_an_agent: str | None
    zugestaendnisse_erkannt: list[str]


class GroupReview(Output):
    gruppen_id: str
    mitglieder: list[str]
    entscheidung: Literal["freigegeben", "abgelehnt"]
    begruendung_intern: str
    begruendung_an_agents: str
    kanal_id: str | None


class ConflictPosition(Output):
    institution: str
    position: str


class Conflict(Output):
    thema: str
    positionen: list[ConflictPosition]


class Summary(Output):
    gemeinsamkeiten: list[str]
    streitpunkte: list[Conflict]
    zugestaendnisse: list[str]


class Proposal(Output):
    runde: int
    sammelstand: Summary
    kompromissvorschlag: str
    begruendung: str


class Rejection(Output):
    agent: str
    grund: str


class Closing(Output):
    status: Literal["konsens", "teilkompromiss"]
    ergebnis: str
    zustimmung: list[str]
    tolerierung: list[str]
    ablehnung: list[Rejection]
    offene_punkte: list[str]
    empfehlung_neustart: Literal["fortsetzen_mit_stand", "kompletter_reset", "keiner"]
    begruendung_empfehlung: str


class Observation(Output):
    gemeinsamkeiten: list[str]
    vorschlaege: list[str]
    konflikte: list[str]
    zugestaendnisse: list[str]
    positionsaenderungen: list[str]
    koalitionen: list[str]
    abstimmung: list[str]
    referenzen: list[int]


class GroupInfo(Output):
    gruppen_id: str
    mitglieder: list[str]


class Concession(Output):
    agent: str
    position: str
    gegenleistung: str


class VoteSummary(Output):
    zustimmen: list[str]
    tolerieren: list[str]
    ablehnen: list[str]


class RoundSummary(Output):
    runde: int
    gruppen: list[GroupInfo]
    kernpunkte: list[str]
    zugestaendnisse: list[Concession]
    abstimmung: VoteSummary


class TurningPoint(Output):
    runde: int
    phase: str
    beschreibung: str


class Statistics(Output):
    runden: int
    rueckweisungen: int
    enthaltungen: int
    gruppenwechsel: int


class Report(Output):
    status: Literal["konsens", "teilkompromiss"]
    ergebnis: str
    verlauf: list[RoundSummary]
    koalitionsbildung: str
    wendepunkte: list[TurningPoint]
    entscheidungsfindung: str
    offene_konflikte: list[str]
    auffaelligkeiten: list[str]
    statistik: Statistics


class Safety(Output):
    institutionell: bool
    keine_personen: bool
    keine_vertraulichen_statuten: bool


SCHEMAS = {
    "frage_stellen": Question,
    "einzelphase": Position,
    "koalitionsphase": Coalition,
    "zustimmung": Consent,
    "gruppennachricht": GroupMessage,
    "gruppenstatement": Statement,
    "abstimmung": Vote,
    "statement_pruefen": Review,
    "gruppe_pruefen": GroupReview,
    "sammelstand_und_vorschlag": Proposal,
    "abschluss": Closing,
    "zwischenstand": Observation,
    "abschlussbericht": Report,
    "sicherheit": Safety,
}


class ActorInput(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=2, max_length=160)
    parent_id: str | None = None
    system_prompt: str = Field(min_length=10, max_length=30000)
    rote_linien: list[str]
    verhandelbar: list[dict]
    db_infos: list[dict] = Field(default_factory=list)
    endpoint_id: str | None = None
    model: str = ""
    demo: bool = False

    modeled: bool = True
    selectable: bool = True
    source_file: str = ""
    source_hash: str = ""
    seed_hash: str = ""
