namespace :billing do
  task :collect_monthly_sales do
    Billing::CollectService.call
  end
end
