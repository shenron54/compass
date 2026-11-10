import httpx

USER_AGENT = "compass/0.1 (personal portfolio tool; +https://github.com/shenron54/compass)"


def client(timeout: float = 60) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT},
                        follow_redirects=True)
