"""Metro area expansion — converts a major city into its surrounding suburbs.

Used by Auto Pilot campaigns when "Include suburbs" is checked. Instead of
searching just "Phoenix, AZ", it expands to Phoenix + Scottsdale + Mesa +
Tempe + Chandler + Gilbert + etc within the metro area.

Pre-built for key BMP markets. For unlisted metros, the campaign falls
back to the city name as-is (Google Places text search still returns
results in the broader area).
"""

import re

# Pre-built metro area mappings — all major US backyard-industry markets
METRO_AREAS: dict[str, list[str]] = {

    # ============ ARIZONA ============
    "phoenix": [
        "Phoenix, AZ", "Scottsdale, AZ", "Mesa, AZ", "Tempe, AZ",
        "Chandler, AZ", "Gilbert, AZ", "Glendale, AZ", "Peoria, AZ",
        "Surprise, AZ", "Goodyear, AZ", "Avondale, AZ", "Buckeye, AZ",
        "Cave Creek, AZ", "Fountain Hills, AZ", "Paradise Valley, AZ",
        "Queen Creek, AZ", "San Tan Valley, AZ", "Anthem, AZ",
        "Litchfield Park, AZ", "Maricopa, AZ", "Casa Grande, AZ",
    ],
    "tucson": [
        "Tucson, AZ", "Oro Valley, AZ", "Marana, AZ", "Sahuarita, AZ",
        "Green Valley, AZ", "Vail, AZ", "Catalina Foothills, AZ",
    ],
    "flagstaff": [
        "Flagstaff, AZ", "Sedona, AZ", "Prescott, AZ", "Prescott Valley, AZ",
        "Cottonwood, AZ", "Camp Verde, AZ",
    ],

    # ============ NEVADA ============
    "las vegas": [
        "Las Vegas, NV", "Henderson, NV", "North Las Vegas, NV",
        "Summerlin, NV", "Spring Valley, NV", "Enterprise, NV",
        "Paradise, NV", "Boulder City, NV", "Mesquite, NV",
    ],
    "reno": [
        "Reno, NV", "Sparks, NV", "Carson City, NV", "Fernley, NV",
    ],

    # ============ TEXAS ============
    "austin": [
        "Austin, TX", "Round Rock, TX", "Cedar Park, TX", "Georgetown, TX",
        "Pflugerville, TX", "Leander, TX", "Kyle, TX", "Buda, TX",
        "Lakeway, TX", "Bee Cave, TX", "Dripping Springs, TX",
        "San Marcos, TX", "Bastrop, TX",
    ],
    "dallas": [
        "Dallas, TX", "Fort Worth, TX", "Plano, TX", "Frisco, TX",
        "McKinney, TX", "Allen, TX", "Arlington, TX", "Irving, TX",
        "Southlake, TX", "Flower Mound, TX", "Prosper, TX",
        "Celina, TX", "Keller, TX", "Colleyville, TX", "Grapevine, TX",
        "Mansfield, TX", "Grand Prairie, TX", "Denton, TX",
        "Lewisville, TX", "Wylie, TX", "Murphy, TX", "Rockwall, TX",
        "Weatherford, TX", "Burleson, TX",
    ],
    "houston": [
        "Houston, TX", "Katy, TX", "Sugar Land, TX", "The Woodlands, TX",
        "Pearland, TX", "League City, TX", "Cypress, TX", "Spring, TX",
        "Tomball, TX", "Conroe, TX", "Missouri City, TX", "Friendswood, TX",
        "Richmond, TX", "Rosenberg, TX", "Humble, TX", "Kingwood, TX",
        "Fulshear, TX", "Magnolia, TX",
    ],
    "san antonio": [
        "San Antonio, TX", "New Braunfels, TX", "Boerne, TX",
        "Helotes, TX", "Schertz, TX", "Cibolo, TX", "Seguin, TX",
        "Bulverde, TX", "Fair Oaks Ranch, TX", "Garden Ridge, TX",
    ],
    "el paso": [
        "El Paso, TX", "Las Cruces, NM", "Horizon City, TX",
    ],

    # ============ FLORIDA ============
    "tampa": [
        "Tampa, FL", "St. Petersburg, FL", "Clearwater, FL",
        "Brandon, FL", "Wesley Chapel, FL", "Riverview, FL",
        "Lutz, FL", "Land O' Lakes, FL", "Palm Harbor, FL",
        "Largo, FL", "Oldsmar, FL", "New Tampa, FL", "Lithia, FL",
    ],
    "orlando": [
        "Orlando, FL", "Winter Park, FL", "Kissimmee, FL",
        "Windermere, FL", "Winter Garden, FL", "Lake Nona, FL",
        "Ocoee, FL", "Clermont, FL", "Sanford, FL", "Altamonte Springs, FL",
        "Lake Mary, FL", "Apopka, FL", "Celebration, FL",
    ],
    "miami": [
        "Miami, FL", "Fort Lauderdale, FL", "Boca Raton, FL",
        "Coral Gables, FL", "Pembroke Pines, FL", "Weston, FL",
        "Plantation, FL", "Davie, FL", "Homestead, FL",
        "Hollywood, FL", "Doral, FL", "Aventura, FL", "Pinecrest, FL",
        "Palmetto Bay, FL", "Coconut Grove, FL", "Key Biscayne, FL",
    ],
    "jacksonville": [
        "Jacksonville, FL", "St. Augustine, FL", "Ponte Vedra Beach, FL",
        "Orange Park, FL", "Fleming Island, FL", "Fernandina Beach, FL",
        "Jacksonville Beach, FL", "Neptune Beach, FL",
    ],
    "naples": [
        "Naples, FL", "Marco Island, FL", "Bonita Springs, FL",
        "Estero, FL", "Fort Myers, FL", "Cape Coral, FL", "Lehigh Acres, FL",
    ],
    "sarasota": [
        "Sarasota, FL", "Bradenton, FL", "Lakewood Ranch, FL",
        "Venice, FL", "Siesta Key, FL", "Nokomis, FL",
    ],
    "palm beach": [
        "West Palm Beach, FL", "Palm Beach Gardens, FL", "Jupiter, FL",
        "Delray Beach, FL", "Boynton Beach, FL", "Wellington, FL",
        "Royal Palm Beach, FL", "Lake Worth, FL",
    ],

    # ============ CALIFORNIA ============
    "los angeles": [
        "Los Angeles, CA", "Pasadena, CA", "Burbank, CA", "Glendale, CA",
        "Santa Monica, CA", "Beverly Hills, CA", "Calabasas, CA",
        "Thousand Oaks, CA", "Simi Valley, CA", "Encino, CA",
        "Sherman Oaks, CA", "Studio City, CA", "Woodland Hills, CA",
        "Arcadia, CA", "San Marino, CA", "La Canada Flintridge, CA",
    ],
    "orange county": [
        "Irvine, CA", "Newport Beach, CA", "Laguna Beach, CA",
        "Huntington Beach, CA", "Costa Mesa, CA", "Anaheim, CA",
        "Fullerton, CA", "Yorba Linda, CA", "Mission Viejo, CA",
        "Rancho Santa Margarita, CA", "San Clemente, CA", "Dana Point, CA",
        "Ladera Ranch, CA", "Lake Forest, CA", "Tustin, CA",
    ],
    "san diego": [
        "San Diego, CA", "Carlsbad, CA", "Encinitas, CA", "Oceanside, CA",
        "Escondido, CA", "Poway, CA", "La Jolla, CA", "Chula Vista, CA",
        "Del Mar, CA", "Rancho Santa Fe, CA", "Solana Beach, CA",
        "Coronado, CA", "El Cajon, CA", "Santee, CA",
    ],
    "inland empire": [
        "Riverside, CA", "Rancho Cucamonga, CA", "Ontario, CA",
        "Corona, CA", "Temecula, CA", "Murrieta, CA", "Redlands, CA",
        "Claremont, CA", "Upland, CA", "Chino Hills, CA",
    ],
    "sacramento": [
        "Sacramento, CA", "Roseville, CA", "Folsom, CA", "Elk Grove, CA",
        "Rocklin, CA", "Granite Bay, CA", "El Dorado Hills, CA",
        "Davis, CA", "Woodland, CA",
    ],
    "san francisco": [
        "San Francisco, CA", "San Jose, CA", "Palo Alto, CA",
        "Mountain View, CA", "Sunnyvale, CA", "Cupertino, CA",
        "Los Gatos, CA", "Saratoga, CA", "Walnut Creek, CA",
        "Danville, CA", "Pleasanton, CA", "Dublin, CA",
        "Fremont, CA", "San Mateo, CA", "Burlingame, CA",
    ],
    "fresno": [
        "Fresno, CA", "Clovis, CA", "Visalia, CA", "Hanford, CA",
    ],
    "bakersfield": [
        "Bakersfield, CA", "Tehachapi, CA",
    ],

    # ============ COLORADO ============
    "denver": [
        "Denver, CO", "Aurora, CO", "Lakewood, CO", "Littleton, CO",
        "Highlands Ranch, CO", "Parker, CO", "Castle Rock, CO",
        "Golden, CO", "Arvada, CO", "Westminster, CO", "Broomfield, CO",
        "Boulder, CO", "Longmont, CO", "Thornton, CO",
    ],
    "colorado springs": [
        "Colorado Springs, CO", "Manitou Springs, CO", "Pueblo, CO",
        "Monument, CO", "Falcon, CO",
    ],

    # ============ GEORGIA ============
    "atlanta": [
        "Atlanta, GA", "Marietta, GA", "Roswell, GA", "Alpharetta, GA",
        "Johns Creek, GA", "Milton, GA", "Kennesaw, GA", "Woodstock, GA",
        "Peachtree City, GA", "Duluth, GA", "Suwanee, GA",
        "Cumming, GA", "Decatur, GA", "Brookhaven, GA",
    ],

    # ============ NORTH CAROLINA ============
    "charlotte": [
        "Charlotte, NC", "Huntersville, NC", "Cornelius, NC",
        "Davidson, NC", "Matthews, NC", "Waxhaw, NC",
        "Fort Mill, SC", "Rock Hill, SC", "Indian Trail, NC",
    ],
    "raleigh": [
        "Raleigh, NC", "Durham, NC", "Chapel Hill, NC", "Cary, NC",
        "Apex, NC", "Wake Forest, NC", "Holly Springs, NC",
    ],

    # ============ TENNESSEE ============
    "nashville": [
        "Nashville, TN", "Franklin, TN", "Brentwood, TN",
        "Murfreesboro, TN", "Hendersonville, TN", "Gallatin, TN",
        "Mt. Juliet, TN", "Spring Hill, TN", "Nolensville, TN",
    ],

    # ============ SOUTH CAROLINA ============
    "charleston": [
        "Charleston, SC", "Mount Pleasant, SC", "Summerville, SC",
        "Goose Creek, SC", "James Island, SC", "Johns Island, SC",
        "Kiawah Island, SC", "Daniel Island, SC",
    ],
    "greenville": [
        "Greenville, SC", "Simpsonville, SC", "Greer, SC",
        "Spartanburg, SC", "Taylors, SC", "Mauldin, SC",
    ],

    # ============ VIRGINIA ============
    "northern virginia": [
        "McLean, VA", "Great Falls, VA", "Vienna, VA", "Reston, VA",
        "Ashburn, VA", "Leesburg, VA", "Fairfax, VA", "Arlington, VA",
        "Alexandria, VA", "Herndon, VA", "Centreville, VA",
    ],

    # ============ MARYLAND ============
    "maryland": [
        "Bethesda, MD", "Potomac, MD", "Rockville, MD", "Silver Spring, MD",
        "Columbia, MD", "Ellicott City, MD", "Annapolis, MD",
    ],

    # ============ NEW JERSEY ============
    "new jersey": [
        "Princeton, NJ", "Hoboken, NJ", "Montclair, NJ",
        "Morristown, NJ", "Ridgewood, NJ", "Short Hills, NJ",
        "Summit, NJ", "Westfield, NJ", "Red Bank, NJ",
    ],

    # ============ CONNECTICUT ============
    "connecticut": [
        "Greenwich, CT", "Westport, CT", "Darien, CT", "New Canaan, CT",
        "Stamford, CT", "Fairfield, CT", "Glastonbury, CT",
    ],

    # ============ NEW YORK ============
    "long island": [
        "Garden City, NY", "Great Neck, NY", "Manhasset, NY",
        "Roslyn, NY", "Oyster Bay, NY", "Cold Spring Harbor, NY",
        "Huntington, NY", "Northport, NY", "Smithtown, NY",
    ],
    "westchester": [
        "Scarsdale, NY", "Bronxville, NY", "Larchmont, NY",
        "Rye, NY", "Chappaqua, NY", "Bedford, NY", "Katonah, NY",
    ],

    # ============ ILLINOIS ============
    "chicago": [
        "Chicago, IL", "Naperville, IL", "Hinsdale, IL", "Lake Forest, IL",
        "Winnetka, IL", "Highland Park, IL", "Barrington, IL",
        "Glen Ellyn, IL", "Wheaton, IL", "Downers Grove, IL",
        "Schaumburg, IL", "Arlington Heights, IL",
    ],

    # ============ MICHIGAN ============
    "detroit": [
        "Detroit, MI", "Birmingham, MI", "Bloomfield Hills, MI",
        "Troy, MI", "Rochester Hills, MI", "Northville, MI",
        "Ann Arbor, MI", "Plymouth, MI", "Grosse Pointe, MI",
    ],

    # ============ OHIO ============
    "columbus": [
        "Columbus, OH", "Dublin, OH", "Westerville, OH", "Powell, OH",
        "New Albany, OH", "Upper Arlington, OH", "Hilliard, OH",
    ],
    "cincinnati": [
        "Cincinnati, OH", "Mason, OH", "West Chester, OH",
        "Indian Hill, OH", "Montgomery, OH", "Blue Ash, OH",
    ],

    # ============ MINNESOTA ============
    "minneapolis": [
        "Minneapolis, MN", "St. Paul, MN", "Edina, MN", "Wayzata, MN",
        "Eden Prairie, MN", "Minnetonka, MN", "Plymouth, MN",
        "Woodbury, MN", "Maple Grove, MN",
    ],

    # ============ WASHINGTON ============
    "seattle": [
        "Seattle, WA", "Bellevue, WA", "Kirkland, WA", "Redmond, WA",
        "Mercer Island, WA", "Issaquah, WA", "Sammamish, WA",
        "Woodinville, WA", "Bothell, WA",
    ],

    # ============ OREGON ============
    "portland": [
        "Portland, OR", "Lake Oswego, OR", "West Linn, OR",
        "Tigard, OR", "Beaverton, OR", "Hillsboro, OR", "Tualatin, OR",
    ],

    # ============ UTAH ============
    "salt lake city": [
        "Salt Lake City, UT", "Park City, UT", "Draper, UT",
        "Sandy, UT", "Lehi, UT", "Orem, UT", "Provo, UT",
        "American Fork, UT", "Highland, UT", "Alpine, UT",
    ],

    # ============ HAWAII ============
    "honolulu": [
        "Honolulu, HI", "Kailua, HI", "Kaneohe, HI",
        "Pearl City, HI", "Kapolei, HI",
    ],

    # ============ INTERNATIONAL (curated anchors) ============
    # Outside the US, "surrounding communities" aren't tidy suburbs — tourist
    # economies spread across a whole region. These curated lists cover the
    # markets we target most; anything else falls through to AI expansion
    # (expand_location_ai), which knows the wider geography of any country.
    "costa rica": [
        "San José, Costa Rica", "Tamarindo, Costa Rica", "Jacó, Costa Rica",
        "Manuel Antonio, Costa Rica", "Quepos, Costa Rica", "La Fortuna, Costa Rica",
        "Monteverde, Costa Rica", "Liberia, Costa Rica", "Nosara, Costa Rica",
        "Santa Teresa, Costa Rica", "Sámara, Costa Rica", "Puerto Viejo, Costa Rica",
        "Tortuguero, Costa Rica", "Uvita, Costa Rica", "Dominical, Costa Rica",
        "Playas del Coco, Costa Rica", "Flamingo, Costa Rica", "Puntarenas, Costa Rica",
        "Drake Bay, Costa Rica", "Montezuma, Costa Rica",
    ],
    "guanacaste": [
        "Tamarindo, Costa Rica", "Playas del Coco, Costa Rica", "Flamingo, Costa Rica",
        "Nosara, Costa Rica", "Sámara, Costa Rica", "Liberia, Costa Rica",
        "Playa Grande, Costa Rica", "Playa Hermosa, Costa Rica", "Potrero, Costa Rica",
    ],
    "cancun": [
        "Cancún, Mexico", "Playa del Carmen, Mexico", "Tulum, Mexico",
        "Cozumel, Mexico", "Isla Mujeres, Mexico", "Puerto Morelos, Mexico",
        "Akumal, Mexico", "Puerto Aventuras, Mexico", "Bacalar, Mexico",
        "Holbox, Mexico",
    ],
    "riviera maya": [
        "Playa del Carmen, Mexico", "Tulum, Mexico", "Puerto Morelos, Mexico",
        "Akumal, Mexico", "Puerto Aventuras, Mexico", "Cozumel, Mexico",
        "Cancún, Mexico",
    ],
    "los cabos": [
        "Cabo San Lucas, Mexico", "San José del Cabo, Mexico",
        "Todos Santos, Mexico", "La Paz, Mexico",
    ],
}


# Every US state, full name + abbreviation, lowercased — used to strip a
# trailing state token off a location string so we can match the city to a
# metro key. Longest-first matching (handled in the regex via sorted len)
# means "new york" wins over a bare "ny" fragment.
_STATE_TOKENS = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
)
# Strip a trailing state preceded by ANY separator — comma, hyphen, or
# whitespace — so 'Miami, FL', 'Miami-FL', 'Miami FL', and 'Miami, Florida'
# all normalize to 'miami'. The required separator + end-anchor means
# hyphenated city names survive: 'Winston-Salem, NC' loses only ', nc'.
_STATE_RE = re.compile(
    r"[\s,\-]+(?:" + "|".join(sorted(_STATE_TOKENS, key=len, reverse=True)) + r")$"
)


def expand_metro(location: str) -> list[str]:
    """If the location matches a known metro area, return all its suburbs;
    otherwise return the location unchanged in a one-item list.

    Normalizes to the city by stripping a trailing US state token regardless
    of separator: 'Miami, FL', 'Miami, Florida', 'Miami-FL', 'Miami FL', and
    'Miami' all resolve to 'miami'. BDRs type locations free-hand, so the
    separator format can't be assumed."""
    raw = location.lower().strip()
    key = _STATE_RE.sub("", raw).strip().rstrip(",").strip()
    if key in METRO_AREAS:
        return METRO_AREAS[key]
    return [location]


def get_available_metros() -> list[dict]:
    """Return the list of available metro areas for the UI dropdown."""
    return [
        {"key": k, "name": k.title(), "cities": len(v), "sample": ", ".join(v[:4]) + f" +{len(v)-4} more" if len(v) > 4 else ", ".join(v)}
        for k, v in METRO_AREAS.items()
    ]


async def expand_location_ai(location: str, *, max_places: int = 30) -> list[str]:
    """Expand ANY location into searchable nearby places via AI — the path
    for international markets the curated METRO_AREAS don't cover.

    For US metros this rarely runs (the dict handles them). For a region or
    country (Costa Rica, Riviera Maya, Latin America), it uses a WIDER net:
    the notable towns and tourist hubs across that area, since businesses are
    spread out rather than clustered in suburbs. Returns [location] on any
    failure so prospecting still works with the bare name."""
    from app.config import settings
    if not (settings.anthropic_api_key or "").strip():
        return [location]
    prompt = (
        "I'm building location targeting for a local-business prospecting tool. "
        f"Expand this location into specific places to search on Google Maps: \"{location}\".\n\n"
        "Rules:\n"
        "- If it's a US metro, return the city + its suburbs.\n"
        "- If it's a region, state/province, or country (e.g. Costa Rica, Riviera Maya, "
        "Guanacaste, Latin America), use a WIDER approach: list the notable towns, beach "
        "areas, and tourist hubs across it where local businesses operate — they're spread "
        "out, not clustered like US suburbs.\n"
        "- Always include the country (or US state) in each entry for geocoding clarity, "
        "e.g. \"Tamarindo, Costa Rica\".\n"
        f"Return ONLY a JSON array of {max_places} or fewer place strings, nothing else."
    )
    try:
        import anthropic, json as _json
        from app.services.ai_client import MODEL_BALANCED
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=MODEL_BALANCED, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if txt.startswith("```"):
            txt = txt.split("```", 2)[1].lstrip("json").strip() if "```" in txt[3:] else txt.strip("`")
        places = _json.loads(txt)
        out = [str(p).strip() for p in places if str(p).strip()][:max_places]
        return out or [location]
    except Exception:
        return [location]


async def expand_location_list(locations: list[str], *, use_ai: bool = True) -> list[str]:
    """Expand a list of locations into a deduped search list. Curated
    METRO_AREAS first (instant, US + key international); for anything that
    doesn't match, fall back to AI (use_ai) for a wider international net.
    The single entry point used by /expand-locations and campaign create/update."""
    expanded: list[str] = []
    for loc in locations:
        metro = expand_metro(loc)
        # expand_metro returns [loc] unchanged when there's no curated match.
        if use_ai and len(metro) == 1 and metro[0] == loc:
            metro = await expand_location_ai(loc)
        expanded.extend(metro)
    seen, unique = set(), []
    for city in expanded:
        k = city.lower().strip()
        if k and k not in seen:
            seen.add(k); unique.append(city)
    return unique
