from enum import StrEnum


class CollectionStatus(StrEnum):
    NOT_FOUND = "not_found"
    SUCCESS = "success"
    PRODUCT_OFFLINE = "product_offline"
    OUT_OF_STOCK = "out_of_stock"
    SKU_MISMATCH = "sku_mismatch"
    STORE_MISMATCH = "store_mismatch"
    STORE_UNVERIFIED = "store_unverified"
    PRICE_AMBIGUOUS = "price_ambiguous"
    LOGIN_REQUIRED = "login_required"
    CHALLENGE_DETECTED = "challenge_detected"
    RATE_LIMITED = "rate_limited"
    PAGE_CHANGED = "page_changed"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    UNKNOWN_ERROR = "unknown_error"


class CalculationStatus(StrEnum):
    SUCCESS = "success"
    MISSING_PACK = "missing_pack"
    PACK_MISMATCH = "pack_mismatch"
    CONTROL_PRICE_AMBIGUOUS = "control_price_ambiguous"
    NOT_APPLICABLE = "not_applicable"


class PriceStatus(StrEnum):
    BELOW_CONTROL = "below_control"
    AT_CONTROL = "at_control"
    ABOVE_CONTROL = "above_control"
    NOT_EVALUATED = "not_evaluated"


class IncidentStatus(StrEnum):
    PENDING_HUMAN = "pending_human"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DEFERRED = "deferred"
    RETRY_READY = "retry_ready"
    ABANDONED = "abandoned"
    SESSION_DISABLED = "session_disabled"


class CandidateType(StrEnum):
    KNOWN_TARGET = "known_target"
    NEW_LINK_SAME_STORE = "new_link_same_store"
    KNOWN_NON_FIXED_STORE = "known_non_fixed_store"
    NEW_STORE = "new_store"
    POSSIBLE_MATCH = "possible_match"
    NOT_MATCH = "not_match"
    INVALID_LINK = "invalid_link"


class FixedTier(StrEnum):
    RESPONSIBILITY_CORE = "responsibility_core"
    OBSERVATION_ONLY = "observation_only"


class TaskType(StrEnum):
    HEALTH_CHECK = "health_check"
    FIXED_CORE = "fixed_core"
    FIXED_OBSERVATION = "fixed_observation"
    SEARCH = "search"
    STORE_SEARCH = "store_search"
    INSPECT_CANDIDATE = "inspect_candidate"


class TaskStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HUMAN_REQUIRED = "human_required"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


class StoreSelectionMode(StrEnum):
    """Store selection scope for store search task generation.

    RESPONSIBILITY_ONLY (default, most conservative):
        Only stores with a drug responsibility relationship AND executable
        platform identity.  Checks both MonitorTarget links and identity status.

    EXECUTABLE_ONLY:
        Only stores with executable platform identity (verified provider_id
        for yaoshibang, shop_home_url for taobao).  No drug relationship
        check — wider than RESPONSIBILITY_ONLY.

    MANUAL:
        Only stores explicitly selected by the user via internal_store_id.

    ALL_DANGER:
        All stores on the platform (requires confirmation dialog).
    """
    RESPONSIBILITY_ONLY = "responsibility_only"
    EXECUTABLE_ONLY = "executable_only"
    MANUAL = "manual"
    ALL_DANGER = "all_danger"
