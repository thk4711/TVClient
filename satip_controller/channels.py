def step(favourites: list[str], current: str, delta: int) -> str:
    if not favourites:
        raise ValueError("No favourites configured")
    index = favourites.index(current) if current in favourites else ( -1 if delta > 0 else 0 )
    return favourites[(index + delta) % len(favourites)]
