"""
Guards against publisher/consumer name drift for Pub/Sub mock messages.

Publishers address mock messages with each consumer service's getProjectName().
Consumers match on their own AppConfig().projectName, which is also the name of
the pubSubMockDb collection their pubSubMockDataGenerator.py writes into. So for
every registered service, getProjectName() must be an existing pubSubMockDb
collection -- otherwise messages addressed to it can never be consumed.

This only proves the collection exists (an empty one still counts -- other
repos' tests routinely wipe their own collection), not that it currently holds
messages; the consumer-side test_ifAllPubSubMockDataHasFunctions covers that.
Renaming a target's projectName leaves the old collection behind, so drop it
or this test keeps passing for the stale name.
"""
import os

from mongoDb import pubSubMockDb
from services.AllServices import serviceGetter

# Services whose target repo has never populated pubSubMockDb. Listing a service
# here is an explicit decision -- never let a mismatch slip through silently.
# >>> star-app per-repo: SERVICES_WITHOUT_MOCK_DATA_YET
SERVICES_WITHOUT_MOCK_DATA_YET: list[str] = ['exampleService']  # remove when you replace the example service
# <<< star-app per-repo: SERVICES_WITHOUT_MOCK_DATA_YET

SERVICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'services')


def test_ifAllServiceFilesAreRegistered():
    """Every services/<name>.py must be registered in services/AllServices.py;
    an unregistered service is invisible to this test and to AppConfig."""
    serviceFiles = {
        f[:-3]
        for f in os.listdir(SERVICES_DIR) if f.endswith('.py')
        and not f.startswith('__') and f != 'AllServices.py'
    }
    registeredNames = {x.name for x in serviceGetter.listOfServices}
    missing = serviceFiles - registeredNames
    assert not missing, (
        f'Service files not registered in services/AllServices.py: '
        f'{sorted(missing)}')


def test_ifAllServicesMatchConsumerProjectName():
    collections = {
        x
        for x in pubSubMockDb.getAllCollectionNames()
        if not x.endswith('_devData')
    }
    services = list(serviceGetter.listOfServices)
    assert len(services) > 0, 'No services registered in services/AllServices.py'

    for service in services:
        if service.name in SERVICES_WITHOUT_MOCK_DATA_YET:
            continue
        projectName = service.getProjectName()
        assert projectName in collections, (
            f"Service '{service.name}' (services/{service.name}.py) addresses "
            f"mock messages to projectName='{projectName}', but no "
            f"'{projectName}' collection exists in pubSubMockDb. Either set "
            f"projectName= on the service to the target repo's "
            f"AppConfig().projectName, or run that repo's "
            f"pubSubMockDataGenerator.py.")


if __name__ == '__main__':
    test_ifAllServiceFilesAreRegistered()
    test_ifAllServicesMatchConsumerProjectName()
