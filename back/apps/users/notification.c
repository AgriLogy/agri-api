
// notification function:
float calculate_irrigation(
    float et0_yesterday,
    float kc,
    float kr,
    float precipitation_yesterday, // cumul de precipitation d'hier
    float soil_moisture, // humidity soil
    float critical_moisture_threshold,
    float zone_area,
    float permeability_loss,
    float irrigation_efficiency, 
    float flow_rate_m3h, // manula input
    float max_water_m3_day, // max li n9dar ns9i f haik zone
    int irrigated_today, // 0 = لم يتم الري, 1 = تم
    int missed_days
) {
    float etc_yesterday = et0_yesterday * kc;

    // إذا لم نسقي البارح ولا اليوم اللي قبل، نضيف ET0 لليومين او اكتر
    if (!irrigated_today && missed_days >= 1) {
        etc_yesterday += et0_day_before, before * kc;
    }

    // نحسب ETc الفعلي
    float effective_ETc = (etc_yesterday - precipitation_yesterday);
   //*
 if (precipitation_forecast > 2) {
        effective_ETc -= precipitation_forecast;
    }

    if (effective_ETc < 0) effective_ETc = 0;
//*
    effective_ETc += permeability_loss;

    // BB = ETC * KR / efficiency
    float bb = (effective_ETc * kr) / (irrigation_efficiency );

    // تحقق من الرطوبة
    if (soil_moisture < critical_moisture_threshold) {
        printf("✅ السقي مطلوب اليوم.\n");

        // كمية المياه المطلوبة بالمتر المكعب
        float irrigation_amount_m3 = (effective_ETc * zone_area) / 1000.0;

        // الحد الأقصى للتطبيق اليومي
        if (irrigation_amount_m3 > max_water_m3_day) {     //اقصى قيمة نقدرو نطبقوها في اليوم
            irrigation_amount_m3 = max_water_m3_day;
            printf("⚠️ تم تجاوز الحد الأقصى للتطبيق اليومي. سيتم تطبيق: f m3 فقط.\n", irrigation_amount_m3);
        }

        // مدة السقي
        float duration_hr = irrigation_amount_m3 / flow_rate_m3h;
        printf("🔸 كمية المياه: %.2f m3\n", irrigation_amount_m3);
        printf("🔸 مدة الري: %.2f ساعة\n", duration_hr);

        // توزيع الري على فترات
        float per_irrigation = irrigation_amount_m3 / interval;
        printf("🕒 تقسيم الري: %.2f m3 صباحًا و %.2f m3 مساءً\n", per_irrigation, per_irrigation);

        return irrigation_amount_m3;
    } else {
        printf("❌ لا حاجة للري اليوم.\n");        // soil_moisture of sensor>critical_moisture_threshold
        return 0;
    }
}
