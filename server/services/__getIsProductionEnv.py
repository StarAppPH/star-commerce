import os


def getIsProductionEnv() -> bool:
    if os.getenv('ENVIRONMENT') in ['localprod', 'cloudprod']:
        return True
    elif os.getenv('ENVIRONMENT') in ['clouddev', 'localdev']:
        return False
    else:
        raise Exception(
            f"Invalid environment: {os.getenv('ENVIRONMENT')!r}. Set ENVIRONMENT "
            "to localdev, localprod, clouddev or cloudprod (locally: "
            "ENVIRONMENT=localdev)")
