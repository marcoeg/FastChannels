"""
Static reference data for resolving US local-news channel origins.

FastChannels stores no market or call-sign field, so origin has to be recovered
from channel names and descriptions. These tables turn the two recoverable
signals — a broadcast call sign, or a place name in the title — into a market.

CALLSIGN_MARKETS covers every call sign observed in the live lineup. It is the
highest-precision signal available and is also authoritative over the source
data: providers occasionally mislabel a market (Sinclair/STIRR describes WPDE
as "Myrtle Beach FL"; Myrtle Beach is in South Carolina), and the call sign
resolves it correctly.

Markets are given as the principal city of the DMA, not the licence city, so
sister stations in one market group together.
"""
from __future__ import annotations

# ── Call sign → (city, state) ────────────────────────────────────────────────
CALLSIGN_MARKETS: dict[str, tuple[str, str]] = {
    "KAAL": ("Rochester", "MN"),        "KATC": ("Lafayette", "LA"),
    "KATU": ("Portland", "OR"),         "KBZK": ("Bozeman", "MT"),
    "KCPQ": ("Seattle", "WA"),          "KCRA": ("Sacramento", "CA"),
    "KDBC": ("El Paso", "TX"),          "KDFW": ("Dallas–Fort Worth", "TX"),
    "KERO": ("Bakersfield", "CA"),      "KEYE": ("Austin", "TX"),
    "KGTV": ("San Diego", "CA"),        "KGUN": ("Tucson", "AZ"),
    "KIRO": ("Seattle", "WA"),          "KIVI": ("Boise", "ID"),
    "KJRH": ("Tulsa", "OK"),            "KMGH": ("Denver", "CO"),
    "KMOV": ("St. Louis", "MO"),        "KMPH": ("Fresno", "CA"),
    "KMSP": ("Minneapolis–St. Paul", "MN"),
    "KMTV": ("Omaha", "NE"),            "KNXV": ("Phoenix", "AZ"),
    "KOAA": ("Colorado Springs", "CO"), "KOB":  ("Albuquerque", "NM"),
    "KOKH": ("Oklahoma City", "OK"),    "KOLD": ("Tucson", "AZ"),
    "KOMO": ("Seattle", "WA"),          "KOTV": ("Tulsa", "OK"),
    "KPAX": ("Missoula", "MT"),         "KPTV": ("Portland", "OR"),
    "KRIS": ("Corpus Christi", "TX"),   "KRIV": ("Houston", "TX"),
    "KRTV": ("Great Falls", "MT"),      "KSAZ": ("Phoenix", "AZ"),
    "KSBW": ("Monterey–Salinas", "CA"), "KSBY": ("San Luis Obispo", "CA"),
    "KSHB": ("Kansas City", "MO"),      "KSL":  ("Salt Lake City", "UT"),
    "KSNV": ("Las Vegas", "NV"),        "KSTP": ("Minneapolis–St. Paul", "MN"),
    "KSTU": ("Salt Lake City", "UT"),   "KTBC": ("Austin", "TX"),
    "KTNV": ("Las Vegas", "NV"),        "KTTV": ("Los Angeles", "CA"),
    "KTVH": ("Helena", "MT"),           "KTVQ": ("Billings", "MT"),
    "KTVU": ("San Francisco Bay Area", "CA"),
    "KUTV": ("Salt Lake City", "UT"),   "KVVU": ("Las Vegas", "NV"),
    "KWCH": ("Wichita", "KS"),          "KWTV": ("Oklahoma City", "OK"),
    "KXTV": ("Sacramento", "CA"),       "KXXV": ("Waco", "TX"),
    "WAGA": ("Atlanta", "GA"),          "WALA": ("Mobile–Pensacola", "AL"),
    "WANF": ("Atlanta", "GA"),          "WAPA": ("San Juan", "PR"),
    "WAVE": ("Louisville", "KY"),       "WBAY": ("Green Bay", "WI"),
    "WBFF": ("Baltimore", "MD"),        "WBRC": ("Birmingham", "AL"),
    "WBTV": ("Charlotte", "NC"),        "WCIV": ("Charleston", "SC"),
    "WCPO": ("Cincinnati", "OH"),       "WDBJ": ("Roanoke", "VA"),
    "WDIO": ("Duluth", "MN"),           "WEWS": ("Cleveland", "OH"),
    "WFLD": ("Chicago", "IL"),          "WFSB": ("Hartford", "CT"),
    "WFTS": ("Tampa Bay", "FL"),        "WFTV": ("Orlando", "FL"),
    "WGBA": ("Green Bay", "WI"),        "WHEC": ("Rochester", "NY"),
    "WHIO": ("Dayton", "OH"),           "WHNS": ("Greenville", "SC"),
    "WHP":  ("Harrisburg", "PA"),       "WITI": ("Milwaukee", "WI"),
    "WJAC": ("Johnstown", "PA"),        "WJAR": ("Providence", "RI"),
    "WJBK": ("Detroit", "MI"),          "WJLA": ("Washington", "DC"),
    "WKBW": ("Buffalo", "NY"),          "WKEF": ("Dayton", "OH"),
    "WKRC": ("Cincinnati", "OH"),       "WKYT": ("Lexington", "KY"),
    "WLEX": ("Lexington", "KY"),        "WLOS": ("Asheville", "NC"),
    "WLUK": ("Green Bay", "WI"),        "WMAR": ("Baltimore", "MD"),
    "WMC":  ("Memphis", "TN"),          "WNYT": ("Albany", "NY"),
    "WNYW": ("New York", "NY"),         "WOAI": ("San Antonio", "TX"),
    "WOFL": ("Orlando", "FL"),          "WOIO": ("Cleveland", "OH"),
    "WPDE": ("Myrtle Beach–Florence", "SC"),
    "WPEC": ("West Palm Beach", "FL"),  "WPTV": ("West Palm Beach", "FL"),
    "WPXI": ("Pittsburgh", "PA"),       "WSB":  ("Atlanta", "GA"),
    "WSBT": ("South Bend", "IN"),       "WSMV": ("Nashville", "TN"),
    "WSOC": ("Charlotte", "NC"),        "WSYX": ("Columbus", "OH"),
    "WTKR": ("Norfolk", "VA"),          "WTMJ": ("Milwaukee", "WI"),
    "WTOV": ("Steubenville–Wheeling", "OH"),
    "WTTG": ("Washington", "DC"),       "WTVF": ("Nashville", "TN"),
    "WTVR": ("Richmond", "VA"),         "WTVT": ("Tampa Bay", "FL"),
    "WTXF": ("Philadelphia", "PA"),     "WTXL": ("Tallahassee", "FL"),
    "WVLT": ("Knoxville", "TN"),        "WVUE": ("New Orleans", "LA"),
    "WWBT": ("Richmond", "VA"),         "WWMT": ("Grand Rapids–Kalamazoo", "MI"),
    "WWSB": ("Sarasota", "FL"),         "WXIX": ("Cincinnati", "OH"),
    "WXMI": ("Grand Rapids", "MI"),     "WXYZ": ("Detroit", "MI"),
}

# ── Place name → (city, state) ───────────────────────────────────────────────
# Matched against the channel name when no call sign is available. Ordered by
# length at match time, so "Kansas City" beats "Kansas" and
# "Minneapolis-St. Paul" beats "Minneapolis".
CITY_MARKETS: dict[str, tuple[str, str]] = {
    "albany": ("Albany", "NY"),                 "albuquerque": ("Albuquerque", "NM"),
    "amarillo": ("Amarillo", "TX"),             "anchorage": ("Anchorage", "AK"),
    "asheville": ("Asheville", "NC"),           "atlanta": ("Atlanta", "GA"),
    "austin": ("Austin", "TX"),                 "bakersfield": ("Bakersfield", "CA"),
    "baltimore": ("Baltimore", "MD"),           "baton rouge": ("Baton Rouge", "LA"),
    "billings": ("Billings", "MT"),             "birmingham": ("Birmingham", "AL"),
    "boise": ("Boise", "ID"),                   "boston": ("Boston", "MA"),
    "bozeman": ("Bozeman", "MT"),               "buffalo": ("Buffalo", "NY"),
    "charleston": ("Charleston", "SC"),         "charlotte": ("Charlotte", "NC"),
    "chattanooga": ("Chattanooga", "TN"),       "chicago": ("Chicago", "IL"),
    "cincinnati": ("Cincinnati", "OH"),         "cleveland": ("Cleveland", "OH"),
    "colorado springs": ("Colorado Springs", "CO"),
    "columbia": ("Columbia", "SC"),             "columbus": ("Columbus", "OH"),
    "corpus christi": ("Corpus Christi", "TX"), "dallas": ("Dallas–Fort Worth", "TX"),
    "dayton": ("Dayton", "OH"),                 "denver": ("Denver", "CO"),
    "des moines": ("Des Moines", "IA"),         "detroit": ("Detroit", "MI"),
    "duluth": ("Duluth", "MN"),                 "el paso": ("El Paso", "TX"),
    "fort myers": ("Fort Myers", "FL"),         "fresno": ("Fresno", "CA"),
    "grand rapids": ("Grand Rapids", "MI"),     "great falls": ("Great Falls", "MT"),
    "green bay": ("Green Bay", "WI"),           "greensboro": ("Greensboro", "NC"),
    "greenville": ("Greenville", "SC"),         "harrisburg": ("Harrisburg", "PA"),
    "hartford": ("Hartford", "CT"),             "helena": ("Helena", "MT"),
    "honolulu": ("Honolulu", "HI"),             "houston": ("Houston", "TX"),
    "huntsville": ("Huntsville", "AL"),         "indianapolis": ("Indianapolis", "IN"),
    "jacksonville": ("Jacksonville", "FL"),     "jax": ("Jacksonville", "FL"),
    "johnstown": ("Johnstown", "PA"),           "kalamazoo": ("Kalamazoo", "MI"),
    "kansas city": ("Kansas City", "MO"),       "knoxville": ("Knoxville", "TN"),
    "lafayette": ("Lafayette", "LA"),           "las vegas": ("Las Vegas", "NV"),
    "lexington": ("Lexington", "KY"),           "little rock": ("Little Rock", "AR"),
    "los angeles": ("Los Angeles", "CA"),       "louisville": ("Louisville", "KY"),
    "lubbock": ("Lubbock", "TX"),               "madison": ("Madison", "WI"),
    "memphis": ("Memphis", "TN"),               "miami": ("Miami", "FL"),
    "milwaukee": ("Milwaukee", "WI"),           "minneapolis": ("Minneapolis–St. Paul", "MN"),
    "missoula": ("Missoula", "MT"),             "mobile": ("Mobile–Pensacola", "AL"),
    "monterey": ("Monterey–Salinas", "CA"),     "myrtle beach": ("Myrtle Beach–Florence", "SC"),
    "nashville": ("Nashville", "TN"),           "new orleans": ("New Orleans", "LA"),
    "new york": ("New York", "NY"),             "norfolk": ("Norfolk", "VA"),
    "oklahoma city": ("Oklahoma City", "OK"),   "omaha": ("Omaha", "NE"),
    "orlando": ("Orlando", "FL"),               "pensacola": ("Mobile–Pensacola", "FL"),
    "philadelphia": ("Philadelphia", "PA"),     "phoenix": ("Phoenix", "AZ"),
    "pittsburgh": ("Pittsburgh", "PA"),         "portland": ("Portland", "OR"),
    "providence": ("Providence", "RI"),         "raleigh": ("Raleigh", "NC"),
    "richmond": ("Richmond", "VA"),             "roanoke": ("Roanoke", "VA"),
    "rochester": ("Rochester", "NY"),           "sacramento": ("Sacramento", "CA"),
    "salinas": ("Monterey–Salinas", "CA"),      "salt lake city": ("Salt Lake City", "UT"),
    "san antonio": ("San Antonio", "TX"),       "san diego": ("San Diego", "CA"),
    "san francisco": ("San Francisco Bay Area", "CA"),
    "san jose": ("San Francisco Bay Area", "CA"),
    "san juan": ("San Juan", "PR"),             "san luis obispo": ("San Luis Obispo", "CA"),
    "sarasota": ("Sarasota", "FL"),             "scranton": ("Scranton", "PA"),
    "seattle": ("Seattle", "WA"),               "south bend": ("South Bend", "IN"),
    "spokane": ("Spokane", "WA"),               "st. louis": ("St. Louis", "MO"),
    "st louis": ("St. Louis", "MO"),            "syracuse": ("Syracuse", "NY"),
    "tallahassee": ("Tallahassee", "FL"),       "tampa": ("Tampa Bay", "FL"),
    "toledo": ("Toledo", "OH"),                 "tucson": ("Tucson", "AZ"),
    "tulsa": ("Tulsa", "OK"),                   "waco": ("Waco", "TX"),
    "washington": ("Washington", "DC"),         "west palm beach": ("West Palm Beach", "FL"),
    "wichita": ("Wichita", "KS"),
}

# Regional and colloquial market names used in channel titles.
REGION_MARKETS: dict[str, tuple[str, str]] = {
    "bay area": ("San Francisco Bay Area", "CA"),
    "central california": ("Fresno", "CA"),
    "central ca": ("Fresno", "CA"),
    "central coast": ("Monterey–Salinas", "CA"),
    "central florida": ("Orlando", "FL"),
    "dallas | fort worth": ("Dallas–Fort Worth", "TX"),
    "dallas-fort worth": ("Dallas–Fort Worth", "TX"),
    "dallas fort worth": ("Dallas–Fort Worth", "TX"),
    "dmv": ("Washington", "DC"),
    "minneapolis-st. paul": ("Minneapolis–St. Paul", "MN"),
    "mobile-pensacola": ("Mobile–Pensacola", "AL"),
    "south florida": ("Miami", "FL"),
    "tampa bay": ("Tampa Bay", "FL"),
    "tri-cities": ("Tri-Cities", "TN"),
    "twin cities": ("Minneapolis–St. Paul", "MN"),
}

# Statewide / multi-state feeds carry no single city. Resolved at state
# granularity and reported as such rather than being guessed into a city.
STATE_MARKETS: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "puerto rico": "PR", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington state": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # Network-specific regional brands with no finer granularity available.
    "carolina": "SC", "new england": "MA",
}

# Spanish-language regional feeds (Telemundo) name a super-region, not a market.
MACRO_REGIONS = {
    "noreste": "Northeast",
    "suroeste": "Southwest",
    "noroeste": "Northwest",
    "sureste": "Southeast",
}
