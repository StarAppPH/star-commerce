"""Payment methods: the ways a store takes payment (its policy's
authorizedPaymentMethods) and the options a customer picks one of at
checkout.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from objects.base import AppModel, ResponseModel


# The ways a store can take payment. Cash, checks and bank transfers are
# paid by hand to the store; cards and crypto through a payment provider;
# e-wallets either way (the store's own accounts, a provider, or both).
PaymentMethod = Literal['cash', 'card', 'eWallet', 'bankTransfer', 'check',
                        'crypto']
# The methods only ever paid by hand, and the ones only ever paid through a
# provider (an eWallet is either).
MANUAL_PAYMENT_METHODS = ('cash', 'bankTransfer', 'check')
PROVIDER_PAYMENT_METHODS = ('card', 'crypto')
# The payment services that take a payment for the store (payments.py): the
# demo one for now. A real one (PayMongo, Maya, Stripe) confirms a payment
# through its webhook.
PaymentProvider = Literal['demo']
# What an order's paymentMethod holds: a PaymentMethod, or the e-wallet an
# order paid before e-wallets were one method was paid with.
OrderPaymentMethod = Literal['cash', 'card', 'eWallet', 'bankTransfer',
                             'check', 'crypto', 'gcash', 'maya', 'grabPay']
PAYMENT_ACCOUNT_NAME_MAX_LENGTH = 60
PAYMENT_ACCOUNT_NUMBER_MAX_LENGTH = 40
CHECK_PAYEE_MAX_LENGTH = 100
# The most accounts (e-wallets, banks, check payees) one method lists.
PAYMENT_ACCOUNTS_MAX = 10

AccountName = Annotated[str, Field(
    min_length=1, max_length=PAYMENT_ACCOUNT_NAME_MAX_LENGTH)]
AccountNumber = Annotated[str, Field(
    min_length=1, max_length=PAYMENT_ACCOUNT_NUMBER_MAX_LENGTH,
    description='The account or mobile number a customer pays to')]
CheckPayee = Annotated[str, Field(
    min_length=1, max_length=CHECK_PAYEE_MAX_LENGTH,
    description='The account name a check is made out to')]
EwalletName = Annotated[AccountName, Field(
    description='The e-wallet, e.g. GCash')]
BankName = Annotated[AccountName, Field(description='The bank, e.g. BDO')]
MethodProvider = Annotated[PaymentProvider | None, Field(
    description='The payment provider that takes it; null: paid by hand to '
    'the store only')]


class EwalletAccount(AppModel):
    """One of the store's own e-wallet accounts a customer pays to by
    hand."""
    ewalletName: EwalletName
    accountNumber: AccountNumber


class BankAccount(AppModel):
    """One of the store's bank accounts a customer transfers to by hand."""
    bankName: BankName
    accountNumber: AccountNumber


def methodProblem(method: str, provider, eWallets, banks,
                  checkPayees) -> str | None:
    """What a payment method misses or has too much of (the details each
    one needs), as the question to ask the admin; None when it holds
    together. Shared by AuthorizedPaymentMethod and the policy route, which
    refuses before building one."""
    details = {'eWallets': eWallets, 'banks': banks,
               'checkPayees': checkPayees}
    needs = {'eWallet': 'eWallets', 'bankTransfer': 'banks',
             'check': 'checkPayees'}.get(method)
    extra = [name for name, value in details.items()
             if value is not None and name != needs]
    if extra:
        return f'{method} takes no {extra[0]}'
    if method in MANUAL_PAYMENT_METHODS and provider is not None:
        return f'{method} is paid by hand to the store: it takes no provider'
    if method in PROVIDER_PAYMENT_METHODS and provider is None:
        return (f'{method} is paid through a payment provider: send its '
                'provider')
    if method == 'eWallet' and provider is None and not eWallets:
        return ('eWallet needs the e-wallet accounts customers pay to '
                '(eWallets: ewalletName and accountNumber), a provider, or '
                'both')
    if method == 'bankTransfer' and not banks:
        return ('bankTransfer needs the bank accounts customers transfer to '
                '(banks: bankName and accountNumber)')
    if method == 'check' and not checkPayees:
        return ('check needs the account names a check is made out to '
                '(checkPayees)')
    return None


class AuthorizedPaymentMethod(AppModel):
    """A way the store takes payment, with what a customer needs to pay
    that way: cash nothing; card and crypto their provider; eWallet the
    store's own accounts, a provider, or both; bankTransfer the store's
    bank accounts; check the names a check is made out to."""
    method: PaymentMethod = Field(..., description='The way to pay')
    provider: MethodProvider = None
    eWallets: list[EwalletAccount] | None = Field(
        None, min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description="eWallet: the store's own accounts, paid to by hand")
    banks: list[BankAccount] | None = Field(
        None, min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description="bankTransfer: the store's bank accounts")
    checkPayees: list[CheckPayee] | None = Field(
        None, min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description='check: the account names a check is made out to')

    @model_validator(mode='after')
    def _holdsTogether(self):
        problem = methodProblem(self.method, self.provider, self.eWallets,
                                self.banks, self.checkPayees)
        if problem is not None:
            raise ValueError(problem)
        return self


class NewEwalletAccount(AppModel):
    """An e-wallet account of an updateMyStorePolicy call. Types only, no
    limits: the route checks them, never quoting a value."""
    ewalletName: str = Field(..., description='The e-wallet, e.g. GCash')
    accountNumber: str = Field(..., description='The account or mobile '
                               'number a customer pays to')


class NewBankAccount(AppModel):
    """A bank account of an updateMyStorePolicy call. Types only, no
    limits: the route checks them, never quoting a value."""
    bankName: str = Field(..., description='The bank, e.g. BDO')
    accountNumber: str = Field(..., description='The account number a '
                               'customer transfers to')


class NewPaymentMethod(AppModel):
    """A payment method of an updateMyStorePolicy call. Types only, no
    limits: the route checks them, so a refusal names the method's
    position and never quotes the value (NewProduct's reason)."""
    method: str = Field(..., description='cash, card, eWallet, '
                        'bankTransfer, check or crypto')
    provider: str | None = Field(
        None, description='The payment provider that takes it (card, '
        'crypto, and optionally eWallet); null: paid by hand to the store')
    eWallets: list[NewEwalletAccount] | None = Field(
        None, description="eWallet: the store's own accounts")
    banks: list[NewBankAccount] | None = Field(
        None, description="bankTransfer: the store's bank accounts")
    checkPayees: list[str] | None = Field(
        None, description='check: the account names a check is made out to')


class EwalletAccountRow(ResponseModel):
    """EwalletAccount as a route sends it. Each field restates
    EwalletAccount's, which test_route_response_models.py's MIRRORS keeps
    in step."""
    ewalletName: EwalletName
    accountNumber: AccountNumber


class BankAccountRow(ResponseModel):
    """BankAccount as a route sends it (MIRRORS keeps it in step)."""
    bankName: BankName
    accountNumber: AccountNumber


class AuthorizedPaymentMethodRow(ResponseModel):
    """AuthorizedPaymentMethod as a route sends it (MIRRORS keeps it in
    step): every detail, null where the method takes none."""
    method: PaymentMethod = Field(..., description='The way to pay')
    provider: MethodProvider = Field(...)
    eWallets: list[EwalletAccountRow] | None = Field(
        ..., min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description="eWallet: the store's own accounts, paid to by hand")
    banks: list[BankAccountRow] | None = Field(
        ..., min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description="bankTransfer: the store's bank accounts")
    checkPayees: list[CheckPayee] | None = Field(
        ..., min_length=1, max_length=PAYMENT_ACCOUNTS_MAX,
        description='check: the account names a check is made out to')


class PaymentOption(ResponseModel):
    """One way a customer can pay an order at checkout, as payOrder takes
    it (paymentMethod and paymentProvider). With a provider it is paid
    there and then; without one it is paid by hand to the store, to the
    accounts listed (none for cash), and the store confirms it."""
    method: PaymentMethod = Field(..., description='The way to pay')
    provider: PaymentProvider | None = Field(
        ..., description='The payment provider that takes it; null: paid by '
        'hand to the store, which then confirms it')
    eWallets: list[EwalletAccountRow] | None = Field(
        ..., description="A manual eWallet: the store's accounts to pay to")
    banks: list[BankAccountRow] | None = Field(
        ..., description="bankTransfer: the store's accounts to transfer to")
    checkPayees: list[str] | None = Field(
        ..., description='check: the names a check is made out to')


def paymentOptions(methods: list[dict] | None) -> list[dict]:
    """The checkout options of the store's authorizedPaymentMethods (as
    stored), in its order: an eWallet with both a provider and accounts is
    two options, the provider's first."""
    options = []
    for method in methods or []:
        if method.get('provider') is not None:
            options.append({'method': method['method'],
                            'provider': method['provider'],
                            'eWallets': None, 'banks': None,
                            'checkPayees': None})
        if method['method'] in MANUAL_PAYMENT_METHODS or method.get(
                'eWallets'):
            options.append({'method': method['method'], 'provider': None,
                            'eWallets': method.get('eWallets'),
                            'banks': method.get('banks'),
                            'checkPayees': method.get('checkPayees')})
    return options
