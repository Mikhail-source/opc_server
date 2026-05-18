local temp = tag_get("Temp_Reactor")
if temp == nil then
    log("⚠️ Tag Temp_Reactor not found or has no value")
    return
end
local setpoint = tag_get("Setpoint_Temp")
local alarm = tag_get("Alarm_HighTemp")

if temp and setpoint then
    -- Гистерезис 5°C
    if temp > setpoint + 5.0 and not alarm then
        tag_set("Alarm_HighTemp", true)
        log("⚠️ HIGH TEMP ALARM: " .. temp .. " > " .. (setpoint + 5))
    elseif temp < setpoint and alarm then
        tag_set("Alarm_HighTemp", false)
        log("✅ Alarm cleared. Temp: " .. temp)
    end
end

-- Скользящее среднее (EMA α=0.2)
local avg = tag_get("Calculated_Avg")
if avg and temp then
    local new_avg = (0.2 * temp) + (0.7 * avg)
    tag_set("Calculated_Avg", new_avg)
end