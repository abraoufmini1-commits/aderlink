"""Analytique : heures de pointe, ventes par heure et par jour, forfaits."""
import time

JOURS = ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche")


def _scope(zone_ids, created_by, zone_col="b.zone_id"):
    sql, params = "", []
    if zone_ids is not None:
        sql += f" AND {zone_col} IN (%s)" % (",".join("?" * len(zone_ids)) or "NULL")
        params += list(zone_ids)
    if created_by:
        sql += " AND b.created_by = ?"
        params.append(created_by)
    return sql, params


def analytics(store, tickets, days, off, zone_ids=None, created_by=None):
    now = int(time.time())
    since = ((now + off) // 86400 - days + 1) * 86400 - off  # minuit local, il y a `days` jours
    off = int(off)

    # 1. Connectés par heure de la journée : moyenne, sur les jours, de la somme des routeurs
    zsql, zparams = _scope(zone_ids, None, "zone_id")
    with store.lock:
        rows = store.db.execute(
            f"SELECT (ts + {off}) / 86400 AS d, ((ts + {off}) / 3600) % 24 AS h, zone_id, AVG(active), MAX(active) "
            f"FROM samples WHERE ts >= ? AND online = 1{zsql} GROUP BY d, h, zone_id", [since] + zparams).fetchall()
    per_dh, peak = {}, {}
    for d, h, _z, avg, mx in rows:
        per_dh[(d, h)] = per_dh.get((d, h), 0) + (avg or 0)
        peak[(d, h)] = peak.get((d, h), 0) + (mx or 0)
    hours = []
    for h in range(24):
        vals = [v for (d, hh), v in per_dh.items() if hh == h]
        pk = [v for (d, hh), v in peak.items() if hh == h]
        hours.append({"hour": h, "avg": round(sum(vals) / len(vals), 1) if vals else 0, "peak": max(pk) if pk else 0})

    # 2. Ventes par heure et par jour de la semaine (1er janvier 1970 = jeudi)
    sc, sp = _scope(zone_ids, created_by)
    base = ("FROM tickets t JOIN batches b ON b.id = t.batch_id "
            "LEFT JOIN forfaits f ON f.id = b.forfait_id WHERE t.first_used >= ?" + sc)
    rows, _ = tickets._q(f"SELECT ((t.first_used + {off}) / 3600) % 24, COUNT(*), COALESCE(SUM(b.price), 0) {base} "
                         "GROUP BY 1", [since] + sp)
    by_hour = {h: (n, a) for h, n, a in rows}
    sales_hour = [{"hour": h, "tickets": by_hour.get(h, (0, 0))[0], "amount": by_hour.get(h, (0, 0))[1]} for h in range(24)]
    rows, _ = tickets._q(f"SELECT ((t.first_used + {off}) / 86400 + 3) % 7, COUNT(*), COALESCE(SUM(b.price), 0) {base} "
                         "GROUP BY 1", [since] + sp)
    by_day = {d: (n, a) for d, n, a in rows}
    # moyenne par jour : on divise par le nombre de ces jours dans la période
    first_day = (since + off) // 86400
    counts = [0] * 7
    for d in range(first_day, (now + off) // 86400 + 1):
        counts[(d + 3) % 7] += 1
    weekdays = [{"day": JOURS[i], "tickets": by_day.get(i, (0, 0))[0], "amount": by_day.get(i, (0, 0))[1],
                 "avg_amount": round(by_day.get(i, (0, 0))[1] / counts[i]) if counts[i] else 0} for i in range(7)]

    # 3. Forfaits : vendus, créés, délai moyen entre la création du lot et la 1re connexion
    rows, _ = tickets._q(
        f"SELECT COALESCE(f.name, b.profile), COUNT(*), COALESCE(SUM(b.price), 0), AVG(CASE WHEN t.first_used >= b.created_at THEN t.first_used - b.created_at END) {base} "
        "GROUP BY 1 ORDER BY 3 DESC", [since] + sp)
    gen, _ = tickets._q(
        "SELECT COALESCE(f.name, b.profile), COUNT(*), SUM(t.first_used IS NULL AND t.removed = 0) "
        "FROM tickets t JOIN batches b ON b.id = t.batch_id LEFT JOIN forfaits f ON f.id = b.forfait_id "
        "WHERE 1=1" + sc + " GROUP BY 1", sp)
    stock = {name: (n, left or 0) for name, n, left in gen}
    forfaits = []
    for name, sold, amount, delay in rows:
        total, left = stock.get(name, (0, 0))
        forfaits.append({"name": name or "–", "sold": sold, "amount": amount, "stock": left,
                         "sell_rate": round((total - left) / total * 100) if total else 0,
                         "delay_days": round(delay / 86400, 1) if delay is not None else None})
    for name, (total, left) in stock.items():  # forfaits sans vente sur la période
        if not any(f["name"] == (name or "–") for f in forfaits):
            forfaits.append({"name": name or "–", "sold": 0, "amount": 0, "stock": left,
                             "sell_rate": round((total - left) / total * 100) if total else 0, "delay_days": None})

    total_t = sum(x["tickets"] for x in sales_hour)
    total_a = sum(x["amount"] for x in sales_hour)
    best_h = max(hours, key=lambda x: x["avg"]) if any(x["avg"] for x in hours) else None
    best_sale_h = max(sales_hour, key=lambda x: x["tickets"]) if total_t else None
    best_d = max(weekdays, key=lambda x: x["avg_amount"]) if total_a else None
    delays = [f["delay_days"] for f in forfaits if f["sold"] and f["delay_days"] is not None]
    return {
        "days": days,
        "hours": hours, "sales_hour": sales_hour, "weekdays": weekdays, "forfaits": forfaits,
        "insights": {
            "peak_hour": best_h["hour"] if best_h else None, "peak_avg": best_h["avg"] if best_h else 0,
            "sales_hour": best_sale_h["hour"] if best_sale_h else None,
            "best_day": best_d["day"] if best_d else None, "best_day_avg": best_d["avg_amount"] if best_d else 0,
            "top_forfait": forfaits[0]["name"] if forfaits and forfaits[0]["sold"] else None,
            "avg_ticket": round(total_a / total_t) if total_t else 0, "tickets": total_t, "amount": total_a,
            "avg_delay": round(sum(delays) / len(delays), 1) if delays else None,
        },
    }
