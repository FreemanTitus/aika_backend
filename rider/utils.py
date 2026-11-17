



def calculate_rider_payout(rider_profile, completed_orders_count, total_cumulative_revenue):
    """
    Calculates payout based on structure rules:
    - Full-Time: ₦1,000 per completed delivery.
    - Casual: 60% Rider / 40% Aika after hitting ₦40,000 cumulative revenue.
    """
    if rider_profile.employment_type == 'full_time':
        return completed_orders_count * 1000.00
    
    elif rider_profile.employment_type == 'casual':
        if total_cumulative_revenue >= 40000.00:
            # 60% split for revenue past threshold or total revenue depending on business rules
            return total_cumulative_revenue * 0.60
        else:
            # Below threshold standard base or lower tier until milestone is cleared
            return total_cumulative_revenue * 0.50  # Example sub-threshold rate
            
    return 0.00