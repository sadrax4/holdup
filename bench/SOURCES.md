# ScamBench sources

Every scam item in `scambench.jsonl` follows a fraud pattern described in one of the public warnings below. This file lists the 53 warnings cited by the 85 scam items, with the exact URL, the page title as published, the publisher, the date shown on the page, the date we read it, the HTTP status of that read and the licence. Benign items have no source.

## How the sources were used

- Every page was downloaded with `curl` on 2026-10-31 (a plain GET, identified as a research source check) and read before it was cited. PDFs were converted to text with `pdftotext`.
- Only public-domain or openly licensed pages were used: US federal government works and UK Crown copyright pages published under the Open Government Licence v3.0.
- No text was copied. Each message was written from scratch to follow the pattern the warning describes, with fictional names, brands, accounts and `.example` links. Before release, the copy check in `validate_scambench.py`, run against the saved text of everything fetched for this work (110 web pages and 3 PDFs), found no message that shares a sequence of six or more consecutive words with any of them.
- The `source_url` of an item is the warning that best describes its pattern; most patterns appear in several warnings.

## Licences

- **US pages (ic3.gov, cftc.gov, investor.gov).** Works of the US federal government are not subject to copyright in the United States (17 U.S.C. 105). The CFTC web policy (https://www.cftc.gov/WebPolicy/index.htm, read 2026-10-31) states that government information on its site is in the public domain and asks for acknowledgement. ic3.gov is operated by the FBI, a component of the Department of Justice, whose legal policies page (https://www.justice.gov/legalpolicies, read 2026-10-31) states that information on its websites is in the public domain unless otherwise indicated; the ic3.gov pages themselves carry no separate licence notice. We found no licence notice on the investor.gov pages read, and sec.gov's own policy page could not be fetched from this environment, so the public-domain status of investor.gov alerts rests on 17 U.S.C. 105 (they are prepared by SEC staff). Some alerts are issued jointly with FINRA or NASAA; we cite them as published on the government site. Logos, images and third-party material on these pages are not covered and were not used.
- **UK pages (GOV.UK, NCSC, Stop! Think Fraud).** The GOV.UK and Stop! Think Fraud page footers state: "All content is available under the Open Government Licence v3.0, except where otherwise stated" (Crown copyright). The NCSC terms and conditions (https://www.ncsc.gov.uk/section/about-this-website/terms-and-conditions, last updated 13 April 2026, read 2026-10-31) state that content is Crown copyright and may be reused under the Open Government Licence v3.0 unless stated otherwise; images credited to third parties are excluded. OGL v3.0: https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/
- **Attribution.** Contains public sector information licensed under the Open Government Licence v3.0. Also draws on public-domain consumer warnings from the FBI Internet Crime Complaint Center, the US Commodity Futures Trading Commission and the US Securities and Exchange Commission. None of these bodies endorses ScamBench.

## Cited sources

### United States: FBI Internet Crime Complaint Center (ic3.gov)

**1. "Phantom Hacker" Scams Target Senior Citizens and Result in Victims Losing their Life Savings**

- URL: https://www.ic3.gov/PSA/2023/PSA230929
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-09-29 (alert I-091223-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Layered tech-support, bank and government impostors tell victims their money is unsafe and must be moved to a 'safe' account, and to keep the real reason secret.
- Items (2): sb-scam-002, sb-scam-003

**2. Technical and Customer Support Fraud**

- URL: https://www.ic3.gov/PSA/2022/PSA220316
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2022-03-16 (alert I-031622-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Impostors posing as exchange or wallet support persuade owners to move wallet contents to another wallet to 'safeguard' them; fake support sites harvest log-in details.
- Items (3): sb-scam-001, sb-scam-005, sb-scam-007

**3. Cybercriminals Trick Victims into Transferring Funds to "Reverse" Instant Payments**

- URL: https://www.ic3.gov/PSA/2022/PSA220414
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2022-04-14 (alert I-041422-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fake bank fraud alerts lead victims to send money in order to 'reverse' a transfer that never happened.
- Items (1): sb-scam-004

**4. FBI Warns of Scammers Impersonating Cryptocurrency Exchanges**

- URL: https://www.ic3.gov/PSA/2024/PSA240801
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-08-01 (alert I-080124-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Callers posing as exchange employees claim an account problem, create urgency and push for log-in details, link clicks or identity documents.
- Items (3): sb-scam-033, sb-scam-035, sb-scam-036

**5. Account Takeover Fraud via Impersonation of Financial Institution Support**

- URL: https://www.ic3.gov/PSA/2025/PSA251125
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-11-25 (alert I-112525-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Account takeover through impostor support staff, lookalike phishing sites and requests for one-time passcodes.
- Items (3): sb-scam-034, sb-scam-037, sb-scam-038

**6. Cryptocurrency Investment Schemes**

- URL: https://www.ic3.gov/PSA/2022/PSA221003
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2022-10-03 (alert I-100322-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Relationship-led crypto investment fraud with fake growing balances; victims who try to cash out are told to pay taxes or fees first.
- Items (1): sb-scam-008

**7. The FBI Warns of a Spike in Cryptocurrency Investment Schemes**

- URL: https://www.ic3.gov/PSA/2023/PSA230314
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-03-14 (alert I-031423-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Crypto investment schemes show fake profits; withdrawals are blocked until a fee or tax is paid, and paying does not release the money.
- Items (1): sb-scam-009

**8. Scammers Target and Exploit Owners of Cryptocurrencies in Liquidity Mining Scam**

- URL: https://www.ic3.gov/PSA/2022/PSA220721
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2022-07-21 (alert I-072122-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Liquidity-mining scam that guarantees one to three percent return per day.
- Items (1): sb-scam-014

**9. Fraudsters Target US Stock Investors through Investment Clubs Accessed on Social Media and Messaging Applications**

- URL: https://www.ic3.gov/PSA/2025/PSA250703
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-07-03 (alert I-070325-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Investment clubs on messaging apps with urgent pitches, promised price rises and guarantees to cover losses.
- Items (1): sb-scam-017

**10. Business Email Compromise: The $55 Billion Scam**

- URL: https://www.ic3.gov/PSA/2024/PSA240911
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-09-11 (alert I-091124-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Business email compromise: verify any change to account details through a second channel and watch for misspelt domains.
- Items (2): sb-scam-026, sb-scam-030

**11. Business Email Compromise: The $50 Billion Scam**

- URL: https://www.ic3.gov/PSA/2023/PSA230609
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-06-09 (alert I-060923-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Business email compromise, including real-estate deals where payment instructions are changed near closing.
- Items (1): sb-scam-031

**12. Business Email Compromise Tactics Used to Facilitate the Acquisition of Commodities and Defrauding Vendors**

- URL: https://www.ic3.gov/PSA/2023/PSA230324
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-03-24 (alert I-032423-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Spoofed lookalike email domains (added words, hyphens, altered spelling) used to deceive vendors.
- Items (2): sb-scam-027, sb-scam-029

**13. Cyber Criminals Impersonating Employee Self-Service Websites to Steal Victim Information and Funds**

- URL: https://www.ic3.gov/PSA/2025/PSA250424
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-04-24 (alert I-042425-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Phishing of employee self-service and payroll sites, passcode theft and redirected direct deposits.
- Items (1): sb-scam-083

**14. Scammers Impersonating Law Enforcement and Government Officials in Fraud Schemes**

- URL: https://www.ic3.gov/PSA/2026/PSA260917
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2026-09-17 (alert I-091726-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Law-enforcement and government impostors threaten arrest, warrants or fines (for example missed jury duty), demand payment including cryptocurrency and insist on secrecy.
- Items (1): sb-scam-039

**15. FBI Warns of the Impersonation of Law Enforcement and Government Officials**

- URL: https://www.ic3.gov/PSA/2022/PSA220307
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2022-03-07 (alert I-030722-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Government impostors use urgent, aggressive tones and demand payment or 'taxes and fees' to clear charges or release funds.
- Items (1): sb-scam-040

**16. Smishing Scam Regarding Debt for Road Toll Services**

- URL: https://www.ic3.gov/PSA/2024/PSA240412
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-04-12 (alert I-041224-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Unpaid-toll text messages with a small balance, a late-fee threat and a lookalike payment link.
- Items (1): sb-scam-044

**17. Scammers Defraud Individuals via Work-From-Home Scams**

- URL: https://www.ic3.gov/PSA/2024/PSA240604
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-06-04 (alert I-060424-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Work-from-home 'optimization' task jobs that require cryptocurrency payments to unlock work or earnings.
- Items (4): sb-scam-047, sb-scam-049, sb-scam-051, sb-scam-052

**18. Fictitious Law Firms Targeting Cryptocurrency Scam Victims Offering to Recover Funds**

- URL: https://www.ic3.gov/PSA/2024/PSA240624
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-06-24 (alert I-062424-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fictitious law firms offer to recover crypto losses in return for upfront fees, back taxes or other charges.
- Items (1): sb-scam-058

**19. Fictitious Law Firms Targeting Cryptocurrency Scam Victims Combine Multiple Exploitation Tactics While Offering to Recover Funds**

- URL: https://www.ic3.gov/PSA/2025/PSA250813
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-08-13 (alert I-081325-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fictitious law firms add foreign-bank accounts, secret group chats and bank or tax fees before 'release'.
- Items (1): sb-scam-062

**20. Increase in Companies Falsely Claiming an Ability to Recover Funds Lost in Cryptocurrency Investment Scams**

- URL: https://www.ic3.gov/PSA/2023/PSA230811
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-08-11 (alert I-081123-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fraudulent crypto tracing and recovery companies charge upfront fees and claim ties to law enforcement or legal services.
- Items (1): sb-scam-060

**21. FBI Warns of Scammers Impersonating the IC3**

- URL: https://www.ic3.gov/PSA/2026/PSA260720
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2026-07-20 (alert I-072026-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Scammers impersonate FBI and IC3 personnel, claim to have recovered victims' money and send links to 'update' complaints.
- Items (1): sb-scam-059

**22. Malicious Actors Manipulating Photos and Videos to Create Explicit Content and Sextortion Schemes**

- URL: https://www.ic3.gov/PSA/2023/PSA230605
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-06-05 (alert I-060523-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Sextortion using manipulated images made from public photos, with payment demanded under threat of sharing.
- Items (1): sb-scam-070

**23. For-Profit Companies Charging Sextortion Victims for Assistance and Using Deceptive Tactics to Elicit Payments**

- URL: https://www.ic3.gov/PSA/2023/PSA230407
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-04-07 (alert I-040723-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: For-profit 'assistance' companies charge sextortion victims upfront fees and discourage them from going to the police.
- Items (1): sb-scam-071

**24. 2023 Holiday Shopping Scams**

- URL: https://www.ic3.gov/PSA/2023/PSA231115
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-11-15 (alert I-111523-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Online selling scams, including buyers who overpay and want the difference sent to a third party.
- Items (5): sb-scam-064, sb-scam-065, sb-scam-066, sb-scam-067, sb-scam-068

**25. FBI Warns of Scammers Targeting Senior Citizens in Grandparent Scams and Demanding Funds by Wire, Mail, or Couriers**

- URL: https://www.ic3.gov/PSA/2023/PSA231117
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-11-17 (alert I-111723-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Grandparent scams: a grandchild in trouble after an accident, an 'attorney' asking for bail money and a demand for secrecy.
- Items (1): sb-scam-077

**26. Beware of Charitable Fraud Related to Mass Casualty and Disaster Events**

- URL: https://www.ic3.gov/PSA/2025/PSA250116
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-01-16 (alert I-011625-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fake charities and crowdfunding after disasters, including cryptocurrency donations.
- Items (1): sb-scam-079

**27. Criminals Use Generative Artificial Intelligence to Facilitate Financial Fraud**

- URL: https://www.ic3.gov/PSA/2024/PSA241203
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2024-12-03 (alert I-120324-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Generative AI in fraud, including cloned voices of relatives asking for immediate help.
- Items (1): sb-scam-085

**28. Cyber Criminals Defraud Hedera Hashgraph Network Non-Custodial Wallet Users Through Nonfungible Token Airdrops Disguised as Free Rewards**

- URL: https://www.ic3.gov/PSA/2025/PSA250603
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-06-03 (alert I-060325-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Fake token airdrops whose claim links ask users to connect wallets and enter seed phrases.
- Items (1): sb-scam-056

**29. Criminals Using Altered Proof-of-Life Media to Extort Victims in Virtual Kidnapping for Ransom Scams**

- URL: https://www.ic3.gov/PSA/2025/PSA251205
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-12-05 (alert I-120525-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Virtual kidnapping ransom demands with altered 'proof of life' images and extreme urgency.
- Items (1): sb-scam-078

**30. Mail Scam Targeting Corporate Executives Claims Ties to Ransomware**

- URL: https://www.ic3.gov/PSA/2025/PSA250306-2
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2025-03-06 (alert I-030625b-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Letters falsely claiming a ransomware group stole company data and demanding bitcoin within ten days.
- Items (1): sb-scam-081

**31. Criminals Pose as Non-Fungible Token (NFT) Developers to Target Internet Users with an Interest in NFT Acquisition**

- URL: https://www.ic3.gov/PSA/2023/PSA230804
- Publisher: FBI Internet Crime Complaint Center (IC3)
- Date on page: 2023-08-04 (alert I-080423-PSA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Cloned NFT project accounts announce 'surprise' mints with urgency and links that drain connected wallets.
- Items (1): sb-scam-084

### United States: Commodity Futures Trading Commission (cftc.gov)

**32. Investor Alert: Relationship Investment Scams**

- URL: https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/RelationshipInvestmentScam.html
- Publisher: US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach
- Date on page: no date shown (joint alert with SEC OIEA, FINRA and NASAA, published on cftc.gov)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Relationship investment scams: fees or taxes demanded at withdrawal, sham 'loans' that must be repaid first, and payments disguised with unrelated notes.
- Items (2): sb-scam-012, sb-scam-019

**33. Customer Advisory: Six Warning Signs of Online Financial Romance Frauds**

- URL: https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/RomanceScam.html
- Publisher: US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach
- Date on page: no date shown (cites 2023 loss figures)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Warning signs of romance-investment fraud; victims are locked out until they pay fees, 'taxes' or identity verification.
- Items (1): sb-scam-011

**34. Don't be Re-Victimized by Recovery Frauds**

- URL: https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/RecoveryFrauds.html
- Publisher: US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach
- Date on page: no date shown
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Recovery frauds as advance-fee schemes: retainers, small fees or taxes demanded before 'recovered' money is released.
- Items (1): sb-scam-063

**35. Curious About Crypto? Watch Out for Red Flags (PDF; title from the file metadata)**

- URL: https://www.cftc.gov/sites/default/files/2022-10/DigitalAssetRedFlags.pdf
- Publisher: US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach
- Date on page: PDF created 2022-09-23, modified 2022-10-01 (no date printed on the page)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Red flags: guaranteed oversized returns, promises to multiply money in days or weeks, and matching or doubling of deposits.
- Items (2): sb-scam-018, sb-scam-053

**36. Customer Advisory - Pause Before You Pay: Unusual Money Transfer Instructions May Signal Fraud (PDF; metadata title: CFTC Pause Before You Pay)**

- URL: https://www.cftc.gov/sites/default/files/2026/08/PaymentMethodsAdvisory082026.pdf
- Publisher: US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach
- Date on page: PDF created 2026-08-25 (no date printed on the page)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Pause before paying: urgent instructions to move money to 'protect' it, callers who stay on the line, and wallet addresses supplied by the caller.
- Items (1): sb-scam-006

### United States: SEC investor education (investor.gov)

**37. 5 Ways Fraudsters May Lure Victims Into Scams Involving Crypto Asset Securities - Investor Alert**

- URL: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/crypto-scams
- Publisher: US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov)
- Date on page: 2024-05-29
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Crypto scam lures: fees or taxes to withdraw, 'frozen by a regulator' claims, 'mistaken deposit' refunds, recovery offers that ask for private keys, deepfakes and hacked friends' accounts.
- Items (4): sb-scam-013, sb-scam-055, sb-scam-057, sb-scam-075

**38. Group Chats as a Gateway to Investment Scams -- Investor Alert**

- URL: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/gateway-to-investment-scams
- Publisher: US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov)
- Date on page: 2025-12-22
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Investment group chats; fees, taxes or deposits demanded to release funds, sometimes with a claim that a regulator froze the account.
- Items (1): sb-scam-010

**39. SEC Impersonators May Lure Investors Into Scams Through Social Media or Text Messages -- Investor Alert**

- URL: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/sec-impersonators-may-lure-investors-scams-through-social-media-or-text-messages-investor-alert
- Publisher: US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov)
- Date on page: 2025-09-30
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Impostors posing as SEC staff offering stock tips, advance-fee deals or help in getting money back.
- Items (1): sb-scam-045

**40. Social Media and Investment Fraud - Investor Alert**

- URL: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/social-media-and-investment-fraud-investor-alert
- Publisher: US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov)
- Date on page: 2022-08-29
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: Social media investment fraud, including fake celebrity endorsements, testimonials and impostor accounts.
- Items (1): sb-scam-054

**41. Artificial Intelligence (AI) and Investment Fraud: Investor Alert**

- URL: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/artificial-intelligence-fraud
- Publisher: US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov)
- Date on page: 2024-01-25 (joint alert with NASAA and FINRA)
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Public domain (US federal government work, 17 U.S.C. 105)
- Pattern used: AI-themed investment pitches such as trading systems that 'can't lose' and guaranteed returns.
- Items (1): sb-scam-016

### United Kingdom: National Cyber Security Centre (ncsc.gov.uk)

**42. Sextortion emails: how to protect yourself**

- URL: https://www.ncsc.gov.uk/guidance/sextortion-scams-how-to-protect-yourself
- Publisher: UK National Cyber Security Centre (NCSC)
- Date on page: published 2018-11-01, reviewed 2022-01-07
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Sextortion emails demanding bitcoin under threat of releasing webcam recordings, sometimes quoting a leaked password.
- Items (2): sb-scam-069, sb-scam-072

**43. How to spot scammers claiming to be from the NCSC**

- URL: https://www.ncsc.gov.uk/guidance/how-to-spot-scammers-claiming-to-be-from-the-ncsc
- Publisher: UK National Cyber Security Centre (NCSC)
- Date on page: published 2023-06-30, reviewed 2024-05-29
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Impostors posing as NCSC officials; genuine officials never ask you to transfer funds, pay a fine or 'assist an investigation' by paying.
- Items (1): sb-scam-043

### United Kingdom: GOV.UK

**44. Guidance on HMCTS related suspicious phone calls, emails, and text messages**

- URL: https://www.gov.uk/guidance/guidance-on-hmcts-related-suspicious-phone-calls-emails-and-text-messages
- Publisher: HM Courts & Tribunals Service (GOV.UK)
- Date on page: published 2023-07-31, last updated 2026-09-17
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Court, bailiff and enforcement-agent impostors demanding immediate transfers for fines or warrants.
- Items (1): sb-scam-042

**45. Warning as investors lose hundreds of thousands of pounds to "Ponzi-style" crypto scheme**

- URL: https://www.gov.uk/government/news/warning-as-investors-lose-hundreds-of-thousands-of-pounds-to-ponzi-style-crypto-scheme
- Publisher: The Insolvency Service (GOV.UK)
- Date on page: 2026-08-18
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Ponzi-style crypto scheme promising guaranteed returns of 40 to 100 percent, pushing recruitment and telling investors to avoid words like 'crypto' in payment references.
- Items (1): sb-scam-015

**46. New Year jobseekers urged to watch out for 7 signs of job scams**

- URL: https://www.gov.uk/government/news/new-year-jobseekers-urged-to-watch-out-for-7-signs-of-job-scams
- Publisher: Disclosure and Barring Service (GOV.UK)
- Date on page: 2023-01-26
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Job scams: offers without interviews and requests to pay before starting, for training, checks or equipment.
- Items (2): sb-scam-048, sb-scam-050

**47. An overseas romance may not be all that it seems...**

- URL: https://www.gov.uk/government/news/an-overseas-romance-may-not-be-all-that-it-seems
- Publisher: Foreign & Commonwealth Office (GOV.UK)
- Date on page: 2012-02-14
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Romance fraud: requests for money for emotive reasons; never send money however persuasive the story; compromising photos used to keep extracting payments.
- Items (5): sb-scam-021, sb-scam-022, sb-scam-023, sb-scam-025, sb-scam-073

**48. Avoiding misleading payment requests**

- URL: https://www.gov.uk/guidance/avoiding-misleading-payment-requests
- Publisher: Intellectual Property Office (GOV.UK)
- Date on page: published 2020-10-06, last updated 2026-06-23
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Misleading invoices from unofficial 'registers' asking for payment to publish or renew trade mark entries.
- Items (1): sb-scam-080

**49. Check if a text message you've received from HMRC is genuine**

- URL: https://www.gov.uk/guidance/check-if-a-text-message-youve-received-from-hmrc-is-genuine
- Publisher: HM Revenue & Customs (GOV.UK)
- Date on page: published 2021-07-30, last updated 2026-10-05
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: HMRC never asks for personal or financial information by text; do not follow links offering tax refunds.
- Items (1): sb-scam-041

**50. DVLA's top tips for avoiding scams**

- URL: https://www.gov.uk/government/news/dvlas-top-tips-for-avoiding-scams
- Publisher: Driver and Vehicle Licensing Agency (GOV.UK)
- Date on page: published 2025-04-04, last updated 2025-12-12
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Vehicle tax refunds are never offered through emailed links, and DVLA never asks for bank details.
- Items (1): sb-scam-046

### United Kingdom: Stop! Think Fraud (stopthinkfraud.campaign.gov.uk)

**51. Recognise the tactics**

- URL: https://stopthinkfraud.campaign.gov.uk/how-to-spot-fraud/recognise-the-tactics/
- Publisher: UK Home Office, Stop! Think Fraud campaign (campaign.gov.uk)
- Date on page: page modified 2026-05-13
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Fraud tactics: a trusted voice, false urgency, emotion (for example a child who has 'had an accident'), current events and relationship-building before a request for money.
- Items (3): sb-scam-020, sb-scam-024, sb-scam-076

**52. Protect your business from fraud**

- URL: https://stopthinkfraud.campaign.gov.uk/protecting-your-business/
- Publisher: UK Home Office, Stop! Think Fraud campaign (campaign.gov.uk)
- Date on page: page modified 2026-05-29
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Payment diversion: CEO fraud, invoice fraud with lookalike domains and changed bank details, and salary diversion; confirm changes on a known number and use delay periods.
- Items (4): sb-scam-028, sb-scam-032, sb-scam-074, sb-scam-082

**53. Be on your guard against further fraud**

- URL: https://stopthinkfraud.campaign.gov.uk/recovery-from-fraud/be-on-your-guard-against-further-fraud/
- Publisher: UK Home Office, Stop! Think Fraud campaign (campaign.gov.uk)
- Date on page: page modified 2024-02-08
- Read: 2026-10-31 with curl (HTTP 200)
- Licence: Open Government Licence v3.0
- Pattern used: Fraud-recovery fraud: genuine agencies never ask for fees to recover money lost to fraud.
- Items (1): sb-scam-061

## Read but not cited

These pages were also read on 2026-10-31 and informed the item design, but no item cites them.

- Scammers Solicit Fake Humanitarian Donations. FBI Internet Crime Complaint Center (IC3), 2023-10-24 (alert I-102423-PSA). https://www.ic3.gov/PSA/2023/PSA231024 (Public domain (US federal government work, 17 U.S.C. 105)). Fake humanitarian charities soliciting cryptocurrency donations during a conflict, sometimes from hacked or spoofed email accounts.
- 10 Signs of a Scam Crypto or Forex Trading Website (PDF). US Commodity Futures Trading Commission (CFTC), Office of Customer Education and Outreach, PDF created 2023-04-20 (no date printed on the page). https://www.cftc.gov/sites/default/files/2023-04/SpotFraudSites.pdf (Public domain (US federal government work, 17 U.S.C. 105)). Signs of a scam trading website, such as no registration, no phone line and a domain younger than the firm claims to be.
- Digital Asset and "Crypto" Investment Scams - Investor Alert. US Securities and Exchange Commission (SEC), Office of Investor Education and Assistance (Investor.gov), 2021-09-01. https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/digital-asset-and-crypto-investment-scams-investor-alert (Public domain (US federal government work, 17 U.S.C. 105)). Digital asset scams: guaranteed high returns and fake testimonials.
- How to spot a scam email, text message or call. UK National Cyber Security Centre (NCSC), published 2021-11-26, reviewed 2022-09-05. https://www.ncsc.gov.uk/collection/phishing-scams/spot-scams (Open Government Licence v3.0). General signs of scam messages: authority, urgency, emotion, scarcity and current events.

## Not read (blocked from this environment)

We did not work around these blocks; nothing from these sites is cited.

- https://consumer.ftc.gov/articles/what-know-about-cryptocurrency-and-scams: FTC consumer advice pages answered plain requests from this environment with HTTP 403 (default client) or a 'Page Not Found' page (HTTP 404). No FTC page was read, so none is cited.
- https://www.fbi.gov/: HTTP 403. FBI pages were not read; FBI material is cited only through ic3.gov.
- https://www.actionfraud.police.uk/: HTTP 403. Not read and not cited (GOV.UK now refers to the successor service as Report Fraud).
- https://www.scamwatch.gov.au/: HTTP 403. Not read and not cited.
- https://www.sec.gov/privacy.htm: HTTP 403 with a rate-threshold page (sec.gov expects a declared contact address in the user agent). SEC material is cited only through investor.gov.

## Unverified points

- Three CFTC advisory pages show no publication date; the dates for the three CFTC PDFs come from the PDF metadata, not from the documents.
- Pages change. Content, titles and dates are as served on 2026-10-31; a later reader may see different text.
- None of the giveaway items has a source that uses the word 'giveaway'. The FTC consumer pages, where we expected the clearest description of giveaway scams, could not be read. Those items cite the CFTC red-flags sheet (promises to multiply or double money), the investor.gov alerts on deepfaked celebrities, hacked friends' accounts and fake endorsements, and the IC3 alert on fake airdrops.
