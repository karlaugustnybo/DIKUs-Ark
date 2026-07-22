from dataclasses import dataclass, field


@dataclass
class HealthResponse:
    status: str
    database: str
    tiles: str


@dataclass
class StatsResponse:
    total: int
    critically_endangered: int
    edge_species: int
    needs_dna_sampling: int
    res3_cells: int
    res7_cells: int


@dataclass
class SpeciesRow:
    species_name: str
    family: str
    redlist_category: str
    threat_score: float
    dna_level: str
    priority: float


@dataclass
class SpeciesPage:
    rows: list[SpeciesRow] = field(default_factory=list)
    page: int = 1
    total_pages: int = 1
    total: int = 0


@dataclass
class CellSpeciesRow:
    species_name: str
    family: str
    redlist_category: str
    dna_level: str


@dataclass
class CellStats:
    total: int = 0
    CR: int = 0
    EN: int = 0
    VU: int = 0
    NT: int = 0
    DD: int = 0
    LC: int = 0
    missing_species_dna: int = 0
    missing_genus_dna: int = 0
    missing_family_dna: int = 0


@dataclass
class CellDetailsResponse:
    h3_index: str
    species: list[CellSpeciesRow] = field(default_factory=list)
    stats: CellStats = field(default_factory=CellStats)


@dataclass
class ExportInfo:
    format: str
    url: str
    media_type: str
