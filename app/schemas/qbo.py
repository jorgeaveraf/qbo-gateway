from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    FieldValidationInfo,
    field_validator,
    model_validator,
)


class QBOProxyResponse(BaseModel):
    client_id: str
    realm_id: str
    environment: str
    fetched_at: datetime
    latency_ms: float
    data: dict[str, Any]
    refreshed: Optional[bool] = None
    idempotent_reuse: bool = False


class QBOListResponse(BaseModel):
    items: list[Any] = Field(default_factory=list)
    next_startposition: Optional[int] = None
    latency_ms: float
    refreshed: Optional[bool] = None


class QBOAccountListResponse(QBOListResponse):
    pass


class QBOAccountDetailResponse(BaseModel):
    account: dict[str, Any]
    latency_ms: float
    refreshed: Optional[bool] = None


class QBOContactAddress(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    line1: str = Field(min_length=1, max_length=500)
    line2: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=100)
    postal_code: Optional[str] = Field(default=None, max_length=50, alias="postal_code")
    country: Optional[str] = Field(default=None, max_length=100)


class QBODocumentLine(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    amount: Decimal = Field(gt=0)
    account_or_item: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=4000)
    class_name: Optional[str] = Field(default=None, alias="class", max_length=255)
    linked_doc_number: Optional[str] = Field(default=None, alias="linked_doc", max_length=100)


class SalesReceiptLine(QBODocumentLine):
    pass


class InvoiceLine(QBODocumentLine):
    pass


class BillLine(QBODocumentLine):
    pass


class DepositLine(QBODocumentLine):
    entity_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Display name of the source entity for the deposit line (matches QuickBooks Received From).",
    )
    entity_type: Optional[Literal["Vendor", "Customer", "Employee", "Other"]] = Field(
        default=None,
        description="Type of the source entity to resolve against QuickBooks (Received From).",
    )

    @model_validator(mode="after")
    def validate_entity_fields(self) -> "DepositLine":
        if self.entity_type and not self.entity_name:
            raise ValueError("entity_name is required when entity_type is provided")
        if self.entity_name and not self.entity_type:
            raise ValueError("entity_type is required when entity_name is provided")
        return self


class PaymentLine(QBODocumentLine):
    linked_doc_number: str = Field(min_length=1, max_length=100, alias="linked_doc")


class BillPaymentLine(QBODocumentLine):
    linked_doc_number: str = Field(min_length=1, max_length=100, alias="linked_doc")


class QBODocumentBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date: date
    doc_number: Optional[str] = Field(default=None, max_length=100)
    private_note: Optional[str] = Field(default=None, max_length=4000)
    class_name: Optional[str] = Field(default=None, alias="class", max_length=255)
    txn_id: Optional[str] = Field(default=None, max_length=150)


class SalesReceiptCreate(QBODocumentBase):
    customer: str = Field(min_length=1, max_length=255)
    lines: list[SalesReceiptLine] = Field(min_length=1)


class InvoiceCreate(QBODocumentBase):
    customer: str = Field(min_length=1, max_length=255)
    lines: list[InvoiceLine] = Field(min_length=1)


class BillCreate(QBODocumentBase):
    vendor: str = Field(min_length=1, max_length=255)
    lines: list[BillLine] = Field(min_length=1)


class DepositCreate(QBODocumentBase):
    deposit_to_account: str = Field(min_length=1, max_length=255)
    lines: list[DepositLine] = Field(min_length=1)


class PaymentCreate(QBODocumentBase):
    customer: str = Field(min_length=1, max_length=255)
    lines: list[PaymentLine] = Field(min_length=1)
    deposit_to_account: Optional[str] = Field(default=None, max_length=255)
    ar_account: Optional[str] = Field(default=None, max_length=255)


class BillPaymentCreate(QBODocumentBase):
    vendor: str = Field(min_length=1, max_length=255)
    lines: list[BillPaymentLine] = Field(min_length=1)
    bank_account: str = Field(min_length=1, max_length=255)
    ap_account: Optional[str] = Field(default=None, max_length=255)
    payment_type: Literal["Check", "CreditCard"] = Field(default="Check")


class ExpenseLine(BaseModel):
    amount: Decimal = Field(gt=0)
    expense_account: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=4000)
    class_name: Optional[str] = Field(default=None, alias="class", max_length=255)


class ExpenseCreate(BaseModel):
    date: date
    vendor: str = Field(min_length=1, max_length=255)
    bank_account: str = Field(min_length=1, max_length=255)
    lines: list[ExpenseLine] = Field(min_length=1)
    private_note: Optional[str] = Field(default=None, max_length=4000)
    doc_number: Optional[str] = Field(default=None, max_length=100)

    @field_validator("lines")
    @classmethod
    def validate_lines_have_amount(cls, value: list[ExpenseLine], info: FieldValidationInfo):
        if not value:
            raise ValueError("At least one expense line is required")
        return value


class CustomerCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=100)
    address: Optional[QBOContactAddress] = None


class VendorCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=100)
    address: Optional[QBOContactAddress] = None


class ItemCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=255)
    type: Literal["Service", "NonInventory", "Inventory"]
    income_account: str = Field(min_length=1, max_length=255)
    expense_account: Optional[str] = Field(default=None, max_length=255)
    asset_account: Optional[str] = Field(default=None, max_length=255)
    quantity_on_hand: Optional[Decimal] = Field(default=None, ge=0)
    inventory_start_date: Optional[date] = Field(default=None)
    description: Optional[str] = Field(default=None, max_length=4000)
    sku: Optional[str] = Field(default=None, max_length=100)
    active: Optional[bool] = True


class AccountUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    account_number: Optional[str] = Field(default=None, alias="account_number", min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=4000)
    active: Optional[bool] = None
    parent_account: Optional[str] = Field(default=None, alias="parent_account", min_length=1, max_length=255)

    @model_validator(mode="after")
    def ensure_any_value(self) -> "AccountUpdate":
        if (
            self.name is None
            and self.description is None
            and self.active is None
            and self.account_number is None
            and self.parent_account is None
        ):
            raise ValueError("At least one updatable field must be provided")
        return self
