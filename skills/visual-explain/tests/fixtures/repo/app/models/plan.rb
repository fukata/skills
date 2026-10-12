class Plan
  # 未払いのときに無料プランへ落とすまでの猶予日数
  GRACE_DAYS = 10

  def renew_plan
    charge_monthly_fee
  end

  def charge_monthly_fee
    true
  end
end
