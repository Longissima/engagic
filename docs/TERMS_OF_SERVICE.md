# Engagic API Terms of Service

**Last Updated:** November 17, 2025

## 1. About Engagic

Engagic is an open-source civic technology platform (AGPL-3.0 licensed) that provides access to public meeting data from local governments across the United States. The code is free and open. The hosted API service (api.engagic.org) is provided as a public good with reasonable usage limits.

## 2. The Deal

**Public data stays public.** We believe government meeting information should be accessible to everyone. We built this infrastructure to make that happen.

**The code is free.** Engagic is AGPL-3.0 licensed. If you want unlimited access and can handle your own infrastructure costs (servers, LLM processing, bandwidth), clone the repo and self-host. No restrictions.

**The hosted API has limits.** Running this service costs real money (LLM processing, server hosting, labor). We provide a free tier for reasonable personal use. Heavy usage requires partnership or payment.

## 3. Usage Tiers

### Free (Basic) Tier
- **Limits:** 30 requests/minute, 300 requests/day
- **Use case:** Personal research, casual browsing, individual civic engagement
- **Cost:** Free forever
- **No registration required**

### Nonprofit/Journalist (Hacktivist) Tier
- **Limits:** 100 requests/minute, 5,000 requests/day
- **Use case:** 501(c)(3) nonprofits, accredited journalists, civic advocacy organizations
- **Requirements:**
  - Proof of nonprofit status or press credentials
  - Public attribution of data source
  - Email contact for partnership
- **Cost:** Free with attribution
- **Contact:** hello@engagic.org

## 4. Attribution Requirements

If you use Engagic data in a public-facing product or publication (website, app, research paper, article), you must provide **clear attribution**:

**Minimum acceptable attribution:**
```
Data provided by Engagic (engagic.org)
```

**Preferred attribution (with link):**
```
Meeting data processed and provided by [Engagic](https://engagic.org),
an open-source civic technology platform.
```

Attribution is waived for:
- Personal/private use (not public-facing)
- Basic tier users (though appreciated)

Attribution is **required** for:
- Nonprofit/journalist tier
- Any commercial use
- Any derivative products/services

## 5. Acceptable Use

**You MAY:**
- Use the API for personal research and civic engagement
- Build tools that help citizens understand local government
- Cite Engagic data in journalism, research, or advocacy
- Self-host the open-source code without restriction (AGPL-3.0)
- Use the data for nonprofit civic tech projects with attribution

**You MAY NOT:**
- Resell raw Engagic data without adding significant value
- Use automated tools to circumvent rate limits
- Use the API in ways that harm civic discourse or spread misinformation
- Claim data as your own without attribution
- Use the service to harass, stalk, or threaten public officials or citizens

## 6. Data Accuracy and Liability

Engagic provides meeting data **AS-IS** with no warranties. While we strive for accuracy:

- Data is sourced from official government websites but may contain errors -- we make every effort to always provide the totality of attachments used to generate the summary, for each item, as well the main overall agenda. 
- LLM-generated summaries are AI-assisted and may be imperfect -- if you see any deviations or hallucinations, please contact us
- Official meeting packets remain the authoritative source -- all information should be doubled checked on your city's website
- We are not liable for decisions made based on Engagic data -- but we sure hope you go out and do good with it

**Always verify important information** with official government sources before taking action.

## 7. Rate Limiting and Enforcement

The API enforces rate limits automatically:

- **Per-minute limits:** Prevent server overload
- **Daily limits:** Ensure fair access for all users
- **429 errors:** Returned when limits exceeded

Repeated rate limit violations or attempts to circumvent limits may result in:
- Temporary IP blocks
- Permanent bans for egregious abuse
- Legal action for damages

We're reasonable, hit us up. 

## 8. Changes to Terms

We may update these terms as the project evolves. Changes will be posted at:
- https://engagic.org/terms
- https://github.com/Engagic/engagic/blob/main/docs/TERMS_OF_SERVICE.md

Continued use of the API after changes constitutes acceptance.

## 9. Open Source License

The Engagic codebase is licensed under **AGPL-3.0**. This means:

- You can view, modify, and distribute the code
- If you run a modified version as a service, you must make your modifications public
- Commercial use of the code is allowed (see tier requirements for hosted API)

Full license: https://github.com/Engagic/engagic/blob/main/LICENSE

## 10. Contact

**Questions, partnerships, or access requests:**
- Email: hello@engagic.org
- GitHub: https://github.com/Engagic/engagic
- Issues: https://github.com/Engagic/engagic/issues

---

## The Philosophy

Civic data should be open. Infrastructure costs money. We're trying to balance both.

If you're a individual wanting to check your city's meetings: **use it freely**.

If you're a nonprofit fighting for housing justice: **let's partner**.

If you're a commercial entity building on our work: **let's talk**.

If you're a VC-backed startup scraping our API to train your models: **pay up or self-host**.

**Questions? hello@engagic.org**

We're humans. Let's talk.
