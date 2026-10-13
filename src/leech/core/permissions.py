import leech.config as config


def is_owner(id: int):
    return id in config.owner_ids
