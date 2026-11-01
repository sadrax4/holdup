# Why not just use something that exists?

HoldUp in brief: a payment to a payee the sender has not trusted waits in HoldVault, a keyless NEAR
contract. An oracle can only make the wait longer (up to 72 hours) and records why on-chain; only
the sender or their guardian can cancel. Trusted payees are paid at once, within a daily cap.
Details: [README](../README.md).

The question this page answers is why that is not already covered by a multisig, a fixed delay, a
blocklist or a custodian. Each comparison below cites a public page; quoted text is copied from the
page as it was read on 2026-10-31. Where a source is secondary, it says so.

## Why not co-signers or a multisig?

**What exists.** In the Argent smart wallet, guardians "can lock, unlock, and trigger a recovery
procedure on the wallet as well as approve the execution of transactions to unknown accounts". A
new trusted contact "takes 36 hours to be effective. Removing an address is triggered by the owner
and is immediate." ([Argent Smart Wallet Specification v2.5][argent])

**The difference.** A co-signer has to be online to approve every payment to someone new. In
HoldUp a routine payment completes on its own after a short wait, and nobody has to approve it; a
guardian only steps in to cancel. The automated checker cannot approve anything: it can only make
one payment wait longer, and it must say why on-chain. Argent wallets are upgradable at the owner's
choice; HoldVault has no upgrade method.

**What HoldUp borrows.** Trust takes effect only after a delay while removal is immediate, and a
change of guardian is delayed too.

## Why not a fixed delay on every payment?

**What exists.**

- The Zodiac Delay Modifier for Safe accounts "allows avatars to enforce a time delay between when
  a module initiates a transaction and when it can be executed by an avatar". The owner can "skip
  transactions by advancing the transaction nonce", and "Anyone can execute the next transaction
  by calling `executeNextTx`". ([README][zodiac])
- Bitcoin's BIP-345 `OP_VAULT` would let users "enforce a delay period before designated coins may
  be spent to an arbitrary destination", with a pre-set recovery path; it is a consensus change and
  its status is Closed. ([BIP-345][bip345])
- Coinbase Vault, as reported in 2014: "a 48-hour delay for withdrawals during which Coinbase will
  try and contact the account holder". This is a secondary source, and the product may have
  changed since. ([TechCrunch, 2 July 2014][techcrunch])

**The difference.** A fixed delay applies the same wait to everything and looks at nothing. HoldUp
holds only payments to payees the sender has not trusted, starts with a short wait (5 minutes on
the hosted bot), and makes a single payment wait longer only when a check finds a reason, which is
recorded on-chain with it. BIP-345 defends against stolen keys by sending coins to a recovery
address; HoldUp defends against payments the owner authorises because someone deceived them, and a
cancel returns the money to the sender's balance. It needs no protocol change: the contract has
been deployed on NEAR testnet as it is.

**What HoldUp borrows.** From the Delay Modifier: once the wait is over, anyone may execute. In
HoldUp anyone may call `release`, and a keeper does it in practice.

## Why not a blocklist or a wallet warning?

**What exists.** Transaction screeners warn before signing. Blockaid: "Simulate and validate every
transaction in real time" and "Identify fund transfers to malicious addresses"
([Blockaid][blockaid]). Pocket Universe: "We show you clear warnings on malicious transactions."
([Pocket Universe][pocketuniverse])

**The difference.** These act inside the wallet before signing, and the user can click past the
warning; once the transfer is sent it is final. Simulating a plain token transfer shows only what
the victim already means to do ("send 50 USDC to X"), because the deception happened earlier, in
the conversation. A list of known bad addresses cannot name an account created two hours ago.
HoldUp adds time after the decision, enforced by a contract, reads the message that prompted the
payment, and looks at the payee's age and activity. Reports against a payee are public contract
state that anyone can read (`get_payee_stats`). A wallet could use both.

## Why not reverse payments afterwards?

**What exists.**

- ERC-20R and ERC-721R: "a transaction is eligible for reversal for a short period of time after it
  has been posted on chain", and a sender asks for a reversal "by convincing a decentralized set of
  judges to first freeze the disputed assets, and then later convincing them to reverse the
  transaction". It is a research proposal with a prototype.
  ([arXiv 2208.00543][erc20r])
- In the UK, payment firms must reimburse victims of authorised push payment scams on "UK bank
  transfers – so when money is moved from one UK bank account to another over the Faster Payments
  system or CHAPS", up to a maximum claim of GBP 85,000.
  ([Payment Systems Regulator][psr])

**The difference.** Reversal acts after settlement, when the payee may already have moved the
money on, so it needs judges, stakes and a new token standard. HoldUp acts before settlement: the
money stays in the vault until the hold ends, so a cancel is a plain refund decided by the sender,
with no judges, and it works with existing NEP-141 tokens. The UK scheme covers bank transfers,
not on-chain payments.

## Why not let a custodian hold the money?

**What exists.** Exchanges and banks hold their customers' money and can delay a payment
themselves. Since 30 October 2024, a UK payment provider with "reasonable grounds to suspect" that
a payment order follows fraud may delay it to make enquiries, "no longer than the end of the fourth
business day following the time of receipt of the payment order", and must tell the payer the
reasons. ([The Payment Services (Amendment) Regulations 2024][si2024])

**The difference.** That rule is the closest analogue to HoldUp: hold a suspicious outbound payment
for a bounded time and tell the payer why. It leaves the decision and the money with the bank. In
HoldUp the money sits in a contract with no keys and no owner, the limits (72 hours at most, the
oracle's single method) are enforced by the contract and by NEAR's access keys, and the reason
codes and the hash of the evidence are on-chain where anyone can read them.

**An honest caveat.** In this demo the bot server derives and holds each user's function-call
key, so HoldUp is not free of trust in its operator: a compromised server could start payments
as its users, which would still wait and could be cancelled. [THREAT_MODEL.md](THREAT_MODEL.md)
describes this and what a production version would change.

## Why not give an AI agent a wallet with spending limits?

**What exists.** OutLayer's Agent Custody gives an AI agent a wallet with "Per-transaction limits,
daily/hourly/monthly caps, address whitelists, time restrictions, rate limits, and multisig
approval", checked inside a trusted execution environment.
([OutLayer docs][outlayer])

**The difference.** There the agent is the spender and the limits bound what it may spend. HoldUp
swaps the roles: a person spends, and the AI may only delay that person's payment. Its limit is a
NEAR function-call key that can call one method, and a contract that accepts only a later release
time.

## Why not NEAR's account recovery timelock?

**What exists.** NEAR's announcement of top-level accounts and recovery keys (21 September 2026)
says a recovery completes only if "A timelock must pass, during which you are notified and can
cancel." ([NEAR on X][near-x])

**The difference.** That timelock protects who controls an account. It does not delay individual
payments. HoldUp's wait applies to each payment to a new payee.

## What is new in HoldUp

These claims are relative to the systems above; we do not claim that nobody anywhere has built any
of them.

1. **The AI checker's only on-chain power is to make a wait longer.** The oracle holds one
   function-call key for `extend_hold`, and the contract accepts only a later release time, at most
   72 hours after the payment (`E_ONLY_LONGER`, `E_ABOVE_MAX_HOLD`). NEAR's docs say such keys "can
   only sign transactions calling a specific contract, and do not allow to attach NEAR tokens to
   the call" ([access keys][near-keys]). In the systems above, screeners warn, agent wallets spend
   within limits, modules queue and execute, judges freeze and reverse, and guardians approve, lock
   and unlock; none holds a power that can only add time.
2. **The reasons and an evidence hash go on-chain when the checker brakes, before any money moves.**
   The contract computes the sha256 of the evidence it receives, and the evidence JSON is published,
   so anyone can re-hash it and compare.
3. **Reports against a payee are public contract state.** The vault counts, for every payee, the
   senders who cancelled a payment after a warning, each sender once. Two such reports, the latest
   within 30 days, make the rules brake the next sender's payment within seconds, with no model
   involved.
4. **The vault has no keys, and anyone can check it.** NEAR's docs say that an account whose keys
   are all removed is locked, "meaning that no external actor can perform transactions in the
   account's name" ([access keys][near-keys]). Locked contracts are a standard NEAR pattern; using
   one for the payment delay itself is what differs from a custodial vault or an upgradable
   wallet.

Not new on its own: a Telegram front end for NEAR. What HoldUp adds there is the flow around the
brake: the user forwards the message behind the payment, every finding says where it came from, and
Cancel and Why buttons sit on the message.

## Sources

- Argent, Smart Wallet Specification v2.5 (23 April 2021): <https://raw.githubusercontent.com/argentlabs/argent-contracts/develop/specifications/specifications.pdf>
- Gnosis Guild, Zodiac Delay Modifier README: <https://raw.githubusercontent.com/gnosisguild/zodiac-modifier-delay/main/README.md>
- BIP-345, OP_VAULT: <https://raw.githubusercontent.com/bitcoin/bips/master/bip-0345.mediawiki>
- TechCrunch, "Coinbase Vault" (2 July 2014; secondary source): <https://techcrunch.com/2014/07/02/coinbase-vault/>
- Blockaid, transaction security: <https://www.blockaid.io/transaction-security>
- Pocket Universe, home page: <https://www.pocketuniverse.app/>
- Wang, Wang and Boneh, ERC-20R and ERC-721R: Reversible Transactions on Ethereum (arXiv 2208.00543): <https://arxiv.org/abs/2208.00543>
- Payment Systems Regulator, APP fraud reimbursement protections: <https://www.psr.org.uk/information-for-consumers/app-fraud-reimbursement-protections/>
- The Payment Services (Amendment) Regulations 2024, SI 2024/1013: <https://www.legislation.gov.uk/uksi/2024/1013/made>
- OutLayer, Agent Custody docs: <https://app.outlayer.ai/docs/agent-custody>
- NEAR Protocol on X, "Introducing Top Level Accounts and Recovery Keys on NEAR" (21 September 2026): <https://x.com/NEARProtocol/status/2102090720223850959>
- NEAR docs, access keys: <https://docs.near.org/protocol/accounts-contracts/access-keys>

[argent]: https://raw.githubusercontent.com/argentlabs/argent-contracts/develop/specifications/specifications.pdf
[zodiac]: https://raw.githubusercontent.com/gnosisguild/zodiac-modifier-delay/main/README.md
[bip345]: https://raw.githubusercontent.com/bitcoin/bips/master/bip-0345.mediawiki
[techcrunch]: https://techcrunch.com/2014/07/02/coinbase-vault/
[blockaid]: https://www.blockaid.io/transaction-security
[pocketuniverse]: https://www.pocketuniverse.app/
[erc20r]: https://arxiv.org/abs/2208.00543
[psr]: https://www.psr.org.uk/information-for-consumers/app-fraud-reimbursement-protections/
[si2024]: https://www.legislation.gov.uk/uksi/2024/1013/made
[outlayer]: https://app.outlayer.ai/docs/agent-custody
[near-x]: https://x.com/NEARProtocol/status/2102090720223850959
[near-keys]: https://docs.near.org/protocol/accounts-contracts/access-keys
