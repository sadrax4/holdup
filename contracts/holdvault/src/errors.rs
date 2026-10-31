//! Stable error codes.
//!
//! Every failure panics with a message of the form `"E_CODE: human text"`, so
//! clients can map errors by prefix without parsing the text.

use near_sdk::env;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    NotRegistered,
    NotSender,
    NotGuardian,
    NotOracle,
    NotToken,
    HoldNotFound,
    HoldNotActive,
    NotDue,
    OnlyLonger,
    AboveMaxHold,
    EvidenceTooLong,
    NoReason,
    InsufficientBalance,
    UnknownAsset,
    ZeroAmount,
    SelfPayment,
    TooManyHolds,
    FeeTooLow,
    Insolvent,
    TooManyTrusted,
    BadGuardian,
    BadConfig,
    MemoTooLong,
    /// `ft_on_transfer` received a `msg` that is neither empty nor `{"credit_to": ...}`.
    BadMessage,
    /// Checked arithmetic failed. Unreachable with real balances; kept as a hard stop.
    Overflow,
}

impl Error {
    pub const fn code(self) -> &'static str {
        match self {
            Error::NotRegistered => "E_NOT_REGISTERED",
            Error::NotSender => "E_NOT_SENDER",
            Error::NotGuardian => "E_NOT_GUARDIAN",
            Error::NotOracle => "E_NOT_ORACLE",
            Error::NotToken => "E_NOT_TOKEN",
            Error::HoldNotFound => "E_HOLD_NOT_FOUND",
            Error::HoldNotActive => "E_HOLD_NOT_ACTIVE",
            Error::NotDue => "E_NOT_DUE",
            Error::OnlyLonger => "E_ONLY_LONGER",
            Error::AboveMaxHold => "E_ABOVE_MAX_HOLD",
            Error::EvidenceTooLong => "E_EVIDENCE_TOO_LONG",
            Error::NoReason => "E_NO_REASON",
            Error::InsufficientBalance => "E_INSUFFICIENT_BALANCE",
            Error::UnknownAsset => "E_UNKNOWN_ASSET",
            Error::ZeroAmount => "E_ZERO_AMOUNT",
            Error::SelfPayment => "E_SELF_PAYMENT",
            Error::TooManyHolds => "E_TOO_MANY_HOLDS",
            Error::FeeTooLow => "E_FEE_TOO_LOW",
            Error::Insolvent => "E_INSOLVENT",
            Error::TooManyTrusted => "E_TOO_MANY_TRUSTED",
            Error::BadGuardian => "E_BAD_GUARDIAN",
            Error::BadConfig => "E_BAD_CONFIG",
            Error::MemoTooLong => "E_MEMO_TOO_LONG",
            Error::BadMessage => "E_BAD_MSG",
            Error::Overflow => "E_OVERFLOW",
        }
    }
}

/// Aborts the call with `"E_CODE: detail"`. All state changes of the call are reverted.
pub fn fail(error: Error, detail: &str) -> ! {
    env::panic_str(&format!("{}: {detail}", error.code()))
}

pub fn ensure(condition: bool, error: Error, detail: &str) {
    if !condition {
        fail(error, detail)
    }
}

/// Checked arithmetic that aborts the call instead of wrapping.
pub trait SafeMath: Sized {
    fn safe_add(self, rhs: Self) -> Self;
    fn safe_sub(self, rhs: Self) -> Self;
    fn safe_mul(self, rhs: Self) -> Self;
}

macro_rules! impl_safe_math {
    ($($t:ty),*) => {$(
        impl SafeMath for $t {
            fn safe_add(self, rhs: Self) -> Self {
                self.checked_add(rhs).unwrap_or_else(|| fail(Error::Overflow, "addition overflow"))
            }

            fn safe_sub(self, rhs: Self) -> Self {
                self.checked_sub(rhs).unwrap_or_else(|| fail(Error::Overflow, "subtraction underflow"))
            }

            fn safe_mul(self, rhs: Self) -> Self {
                self.checked_mul(rhs).unwrap_or_else(|| fail(Error::Overflow, "multiplication overflow"))
            }
        }
    )*};
}

impl_safe_math!(u32, u64, u128);
