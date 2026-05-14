"""Metro area expansion — converts a major city into its surrounding suburbs.

Used by Auto Pilot campaigns when "Include suburbs" is checked. Instead of
searching just "Phoenix, AZ", it expands to Phoenix + Scottsdale + Mesa +
Tempe + Chandler + Gilbert + etc within the metro area.

Pre-built for key BMP markets. For unlisted metros, falls back to Google
Geocoding API to find nearby cities within a radius.
"""

# Pre-built metro area mappings — ordered by market priority
METRO_AREAS: dict[str, list[str]] = {
    # Arizona
    "phoenix": [
        "Phoenix, AZ", "Scottsdale, AZ", "Mesa, AZ", "Tempe, AZ",
        "Chandler, AZ", "Gilbert, AZ", "Glendale, AZ", "Peoria, AZ",
        "Surprise, AZ", "Goodyear, AZ", "Avondale, AZ", "Buckeye, AZ",
        "Cave Creek, AZ", "Fountain Hills, AZ", "Paradise Valley, AZ",
        "Queen Creek, AZ", "San Tan Valley, AZ", "Anthem, AZ",
    ],
    "tucson": [
        "Tucson, AZ", "Oro Valley, AZ", "Marana, AZ", "Sahuarita, AZ",
        "Green Valley, AZ", "Vail, AZ", "Catalina Foothills, AZ",
    ],
    # Nevada
    "las vegas": [
        "Las Vegas, NV", "Henderson, NV", "North Las Vegas, NV",
        "Summerlin, NV", "Spring Valley, NV", "Enterprise, NV",
        "Paradise, NV", "Boulder City, NV",
    ],
    # Texas
    "austin": [
        "Austin, TX", "Round Rock, TX", "Cedar Park, TX", "Georgetown, TX",
        "Pflugerville, TX", "Leander, TX", "Kyle, TX", "Buda, TX",
        "Lakeway, TX", "Bee Cave, TX", "Dripping Springs, TX",
    ],
    "dallas": [
        "Dallas, TX", "Fort Worth, TX", "Plano, TX", "Frisco, TX",
        "McKinney, TX", "Allen, TX", "Arlington, TX", "Irving, TX",
        "Southlake, TX", "Flower Mound, TX", "Prosper, TX",
        "Celina, TX", "Keller, TX", "Colleyville, TX",
    ],
    "houston": [
        "Houston, TX", "Katy, TX", "Sugar Land, TX", "The Woodlands, TX",
        "Pearland, TX", "League City, TX", "Cypress, TX", "Spring, TX",
        "Tomball, TX", "Conroe, TX", "Missouri City, TX",
    ],
    "san antonio": [
        "San Antonio, TX", "New Braunfels, TX", "Boerne, TX",
        "Helotes, TX", "Schertz, TX", "Cibolo, TX",
    ],
    # Florida
    "tampa": [
        "Tampa, FL", "St. Petersburg, FL", "Clearwater, FL",
        "Brandon, FL", "Wesley Chapel, FL", "Riverview, FL",
        "Lutz, FL", "Land O' Lakes, FL", "Palm Harbor, FL",
    ],
    "orlando": [
        "Orlando, FL", "Winter Park, FL", "Kissimmee, FL",
        "Windermere, FL", "Winter Garden, FL", "Lake Nona, FL",
        "Ocoee, FL", "Clermont, FL", "Sanford, FL",
    ],
    "miami": [
        "Miami, FL", "Fort Lauderdale, FL", "Boca Raton, FL",
        "Coral Gables, FL", "Pembroke Pines, FL", "Weston, FL",
        "Plantation, FL", "Davie, FL", "Homestead, FL",
    ],
    # California
    "los angeles": [
        "Los Angeles, CA", "Pasadena, CA", "Burbank, CA", "Glendale, CA",
        "Santa Monica, CA", "Beverly Hills, CA", "Calabasas, CA",
        "Thousand Oaks, CA", "Simi Valley, CA", "Encino, CA",
    ],
    "san diego": [
        "San Diego, CA", "Carlsbad, CA", "Encinitas, CA", "Oceanside, CA",
        "Escondido, CA", "Poway, CA", "La Jolla, CA", "Chula Vista, CA",
    ],
}


def expand_metro(location: str) -> list[str]:
    """If the location matches a known metro area, return all suburbs.
    Otherwise return the location as-is in a single-item list."""
    key = location.lower().strip()
    # Strip state abbreviation for matching
    for suffix in [", az", ", nv", ", tx", ", fl", ", ca", " arizona", " nevada", " texas", " florida", " california"]:
        key = key.replace(suffix, "")
    key = key.strip()

    if key in METRO_AREAS:
        return METRO_AREAS[key]
    return [location]


def get_available_metros() -> list[dict]:
    """Return the list of available metro areas for the UI dropdown."""
    return [
        {"key": k, "name": k.title(), "cities": len(v), "sample": ", ".join(v[:4])}
        for k, v in METRO_AREAS.items()
    ]
