from services.__abstractService import AbstractService
from services.exampleService import exampleService


class ServiceGetter:

    def __init__(self, listOfServices: list[AbstractService]):
        self.listOfServices = listOfServices

    def getServiceByName(self, serviceName: str) -> AbstractService:
        for x in self.listOfServices:
            if x.__class__.__name__.lower() == serviceName.lower():
                return x
        raise ValueError(f"Service '{serviceName}' not found")


serviceGetter = ServiceGetter(
    listOfServices=[exampleService])
