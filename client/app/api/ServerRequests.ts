import Server from "./Server";
import type { ApiResponse } from "./ApiResponse";
import type { AddressSuggestionResponse } from '../schemas/AddressSuggestionResponseSchema';
import type { AdminProductListResponse } from '../schemas/AdminProductListResponseSchema';
import type { AdminProductRow } from '../schemas/AdminProductRowSchema';
import type { ChatReplyResponse } from '../schemas/ChatReplyResponseSchema';
import type { ChatThinkingResponse } from '../schemas/ChatThinkingResponseSchema';
import type { ConversationListResponse } from '../schemas/ConversationListResponseSchema';
import type { ConversationTurnsResponse } from '../schemas/ConversationTurnsResponseSchema';
import type { CreatedOrderResponse } from '../schemas/CreatedOrderResponseSchema';
import type { CustomerSessionResponse } from '../schemas/CustomerSessionResponseSchema';
import type { DeliveryPinAnswer } from '../schemas/DeliveryPinAnswerSchema';
import type { DeliveryZonesAnswer } from '../schemas/DeliveryZonesAnswerSchema';
import type { DeliveryZonesResponse } from '../schemas/DeliveryZonesResponseSchema';
import type { DevUserListResponse } from '../schemas/DevUserListResponseSchema';
import type { FirebaseUserObject } from '../schemas/FirebaseUserObjectSchema';
import type { GeneratedPhotosResponse } from '../schemas/GeneratedPhotosResponseSchema';
import type { HandoverAnswer } from '../schemas/HandoverAnswerSchema';
import type { LocationAnswer } from '../schemas/LocationAnswerSchema';
import type { MyOrderListResponse } from '../schemas/MyOrderListResponseSchema';
import type { MyOrderRow } from '../schemas/MyOrderRowSchema';
import type { MyReturnListResponse } from '../schemas/MyReturnListResponseSchema';
import type { MyReturnRow } from '../schemas/MyReturnRowSchema';
import type { MyStoreResponse } from '../schemas/MyStoreResponseSchema';
import type { NewDeliveryZone } from '../schemas/NewDeliveryZoneSchema';
import type { NewOrderDelivery } from '../schemas/NewOrderDeliverySchema';
import type { NewOrderItem } from '../schemas/NewOrderItemSchema';
import type { NewProduct } from '../schemas/NewProductSchema';
import type { NewProductsAnswer } from '../schemas/NewProductsAnswerSchema';
import type { NewReturnItem } from '../schemas/NewReturnItemSchema';
import type { NoContentResponse } from '../schemas/NoContentResponseSchema';
import type { OrderItemsAnswer } from '../schemas/OrderItemsAnswerSchema';
import type { PaymentAnswer } from '../schemas/PaymentAnswerSchema';
import type { PhoneVerifiedAnswer } from '../schemas/PhoneVerifiedAnswerSchema';
import type { PhoneVerifiedResponse } from '../schemas/PhoneVerifiedResponseSchema';
import type { PhotoRequestAnswer } from '../schemas/PhotoRequestAnswerSchema';
import type { ProductListResponse } from '../schemas/ProductListResponseSchema';
import type { ProductPhotoAnswer } from '../schemas/ProductPhotoAnswerSchema';
import type { ProductPhotoChoicesResponse } from '../schemas/ProductPhotoChoicesResponseSchema';
import type { ReturnDecisionAnswer } from '../schemas/ReturnDecisionAnswerSchema';
import type { RoleListResponse } from '../schemas/RoleListResponseSchema';
import type { SpokenReplyResponse } from '../schemas/SpokenReplyResponseSchema';
import type { SpokenThinkingResponse } from '../schemas/SpokenThinkingResponseSchema';
import type { StoreOrderListResponse } from '../schemas/StoreOrderListResponseSchema';
import type { StoreOrderRow } from '../schemas/StoreOrderRowSchema';
import type { StorePolicyResponse } from '../schemas/StorePolicyResponseSchema';
import type { StoreResponse } from '../schemas/StoreResponseSchema';
import type { StoreReturnListResponse } from '../schemas/StoreReturnListResponseSchema';
import type { StoreReturnRow } from '../schemas/StoreReturnRowSchema';
import type { StoreThemeResponse } from '../schemas/StoreThemeResponseSchema';
import type { UserResponse } from '../schemas/UserResponseSchema';
import type { VoiceListResponse } from '../schemas/VoiceListResponseSchema';
import type { VoiceReplyResponse } from '../schemas/VoiceReplyResponseSchema';

    class ServerRequests extends Server {
    constructor(environment : string) {
        super(environment);
    }


    async loginWithGoogle(firebaseUserObject: FirebaseUserObject): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/loginWithGoogle`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"firebaseUserObject": firebaseUserObject}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devGetUserList(): Promise<ApiResponse<DevUserListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devGetUserList`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({}),
            cache: 'no-store'
        });
        const data: ApiResponse<DevUserListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devLogin(userId: string): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devLogin`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getRoles(userId: string): Promise<ApiResponse<RoleListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getRoles`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<RoleListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getUserRoleTypes(userId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getUserRoleTypes`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getUserStore(userId: string): Promise<ApiResponse<MyStoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getUserStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyStoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async updateMyStoreTheme(userId: string, photoStyle: string | null, primaryColor: string | null, secondaryColor: string | null, voiceId: string | null): Promise<ApiResponse<StoreThemeResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateMyStoreTheme`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "photoStyle": photoStyle, "primaryColor": primaryColor, "secondaryColor": secondaryColor, "voiceId": voiceId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreThemeResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async createUserStore(userId: string, name: string, description: string, contactNumber: string, primaryColor: string, secondaryColor: string, currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED', openingHours: string | null, address: string | null): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createUserStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "name": name, "description": description, "contactNumber": contactNumber, "primaryColor": primaryColor, "secondaryColor": secondaryColor, "currency": currency, "openingHours": openingHours, "address": address}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async updateMyStoreDetails(userId: string, name: string | null, description: string | null, contactNumber: string | null, address: string | null, openingHours: string | null): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateMyStoreDetails`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "name": name, "description": description, "contactNumber": contactNumber, "address": address, "openingHours": openingHours}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async suggestStoreAddress(userId: string): Promise<ApiResponse<AddressSuggestionResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/suggestStoreAddress`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<AddressSuggestionResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async setUserStoreLocation(userId: string, latitude: number, longitude: number): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/setUserStoreLocation`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "latitude": latitude, "longitude": longitude}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async setMyDeliveryZones(userId: string, zones: NewDeliveryZone[]): Promise<ApiResponse<DeliveryZonesResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/setMyDeliveryZones`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "zones": zones}),
            cache: 'no-store'
        });
        const data: ApiResponse<DeliveryZonesResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async resetMyStore(userId: string, confirmName: string): Promise<ApiResponse<MyStoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/resetMyStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "confirmName": confirmName}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyStoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async startGuestSession(idToken: string, storeSlug: string): Promise<ApiResponse<CustomerSessionResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/startGuestSession`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"idToken": idToken, "storeSlug": storeSlug}),
            cache: 'no-store'
        });
        const data: ApiResponse<CustomerSessionResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async verifyCustomerPhone(userId: string, idToken: string): Promise<ApiResponse<PhoneVerifiedResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/verifyCustomerPhone`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "idToken": idToken}),
            cache: 'no-store'
        });
        const data: ApiResponse<PhoneVerifiedResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listStoreProducts(userId: string, storeId: string, search: string | null, limit: number): Promise<ApiResponse<ProductListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listStoreProducts`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "search": search, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ProductListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async addStoreProduct(userId: string, name: string, price: number, inventory: number, description: string | null, isAvailable: boolean): Promise<ApiResponse<AdminProductRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/addStoreProduct`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "name": name, "price": price, "inventory": inventory, "description": description, "isAvailable": isAvailable}),
            cache: 'no-store'
        });
        const data: ApiResponse<AdminProductRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async addStoreProducts(userId: string, products: NewProduct[]): Promise<ApiResponse<AdminProductListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/addStoreProducts`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "products": products}),
            cache: 'no-store'
        });
        const data: ApiResponse<AdminProductListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async updateStoreProduct(userId: string, productId: string, name: string | null, description: string | null, price: number | null, inventory: number | null, isAvailable: boolean | null): Promise<ApiResponse<AdminProductRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateStoreProduct`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "productId": productId, "name": name, "description": description, "price": price, "inventory": inventory, "isAvailable": isAvailable}),
            cache: 'no-store'
        });
        const data: ApiResponse<AdminProductRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listMyStoreProducts(userId: string, search: string | null, limit: number): Promise<ApiResponse<AdminProductListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listMyStoreProducts`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "search": search, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<AdminProductListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getStoreOrders(userId: string, status: string | null, period: string | null, startDate: string | null, endDate: string | null, limit: number): Promise<ApiResponse<StoreOrderListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getStoreOrders`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "status": status, "period": period, "startDate": startDate, "endDate": endDate, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreOrderListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getMyOrders(userId: string, storeId: string, status: string | null, limit: number): Promise<ApiResponse<MyOrderListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getMyOrders`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "status": status, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyOrderListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async createOrder(userId: string, storeId: string, items: NewOrderItem[], fulfillment: 'pickup' | 'delivery' | null, delivery: NewOrderDelivery | null): Promise<ApiResponse<CreatedOrderResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "items": items, "fulfillment": fulfillment, "delivery": delivery}),
            cache: 'no-store'
        });
        const data: ApiResponse<CreatedOrderResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async payOrder(userId: string, storeId: string, orderId: string, paymentMethod: string): Promise<ApiResponse<MyOrderRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/payOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "orderId": orderId, "paymentMethod": paymentMethod}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyOrderRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async completeOrder(userId: string, orderId: string): Promise<ApiResponse<StoreOrderRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/completeOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "orderId": orderId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreOrderRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async cancelStoreOrder(userId: string, orderId: string): Promise<ApiResponse<StoreOrderRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/cancelStoreOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "orderId": orderId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreOrderRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async cancelMyOrder(userId: string, storeId: string, orderId: string): Promise<ApiResponse<MyOrderRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/cancelMyOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "orderId": orderId}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyOrderRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async requestReturn(userId: string, storeId: string, orderId: string, items: NewReturnItem[], reason: string, note: string | null): Promise<ApiResponse<MyReturnRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/requestReturn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "orderId": orderId, "items": items, "reason": reason, "note": note}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyReturnRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getMyReturns(userId: string, storeId: string, limit: number | null): Promise<ApiResponse<MyReturnListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getMyReturns`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyReturnListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async cancelMyReturn(userId: string, storeId: string, returnId: string): Promise<ApiResponse<MyReturnRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/cancelMyReturn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "returnId": returnId}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyReturnRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getStoreReturns(userId: string, status: string | null, limit: number | null): Promise<ApiResponse<StoreReturnListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getStoreReturns`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "status": status, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreReturnListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async approveReturn(userId: string, returnId: string): Promise<ApiResponse<StoreReturnRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/approveReturn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "returnId": returnId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreReturnRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async rejectReturn(userId: string, returnId: string, reason: string): Promise<ApiResponse<StoreReturnRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/rejectReturn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "returnId": returnId, "reason": reason}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreReturnRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async receiveReturn(userId: string, returnId: string, restock: boolean): Promise<ApiResponse<StoreReturnRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/receiveReturn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "returnId": returnId, "restock": restock}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreReturnRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getMyStorePolicy(userId: string): Promise<ApiResponse<StorePolicyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getMyStorePolicy`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StorePolicyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getStorePolicy(userId: string, storeId: string): Promise<ApiResponse<StorePolicyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getStorePolicy`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StorePolicyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async updateMyStorePolicy(userId: string, acceptsReturns: boolean | null, returnWindowDays: number | null, returnConditions: string | null, nonReturnableItems: string | null, refundMethod: string | null, returnShippingPaidBy: string | null, defectReportDays: number | null, deliveryOptions: string[] | null, deliveryAreas: string | null, handoverDays: number | null, acceptedPaymentMethods: string[] | null, paidOrderCancellation: string | null, paidOrderCancellationHours: number | null, additionalTerms: string | null, clear: string[] | null): Promise<ApiResponse<StorePolicyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateMyStorePolicy`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "acceptsReturns": acceptsReturns, "returnWindowDays": returnWindowDays, "returnConditions": returnConditions, "nonReturnableItems": nonReturnableItems, "refundMethod": refundMethod, "returnShippingPaidBy": returnShippingPaidBy, "defectReportDays": defectReportDays, "deliveryOptions": deliveryOptions, "deliveryAreas": deliveryAreas, "handoverDays": handoverDays, "acceptedPaymentMethods": acceptedPaymentMethods, "paidOrderCancellation": paidOrderCancellation, "paidOrderCancellationHours": paidOrderCancellationHours, "additionalTerms": additionalTerms, "clear": clear}),
            cache: 'no-store'
        });
        const data: ApiResponse<StorePolicyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendAdminChatMessage(userId: string, message: string | null, conversationId: string | null, form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | null, attachments: string[] | null, turnKey: string | null, speak: boolean | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendAdminChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "message": message, "conversationId": conversationId, "form": form, "attachments": attachments, "turnKey": turnKey, "speak": speak}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendStoreChatMessage(userId: string, storeId: string, message: string | null, conversationId: string | null, form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | null, attachments: string[] | null, turnKey: string | null, speak: boolean | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendStoreChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "message": message, "conversationId": conversationId, "form": form, "attachments": attachments, "turnKey": turnKey, "speak": speak}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getChatThinking(userId: string, turnKey: string): Promise<ApiResponse<ChatThinkingResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getChatThinking`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatThinkingResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listMyConversations(userId: string, storeId: string | null, before: string | null, limit: number): Promise<ApiResponse<ConversationListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listMyConversations`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "before": before, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ConversationListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getConversationTurns(userId: string, conversationId: string, beforeSeq: number | null, limit: number): Promise<ApiResponse<ConversationTurnsResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getConversationTurns`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "conversationId": conversationId, "beforeSeq": beforeSeq, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ConversationTurnsResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendAdminVoiceMessage(userId: string, audio: string, conversationId: string | null, turnKey: string | null): Promise<ApiResponse<VoiceReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendAdminVoiceMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "audio": audio, "conversationId": conversationId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<VoiceReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendStoreVoiceMessage(userId: string, storeId: string, audio: string, conversationId: string | null, turnKey: string | null): Promise<ApiResponse<VoiceReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendStoreVoiceMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "audio": audio, "conversationId": conversationId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<VoiceReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async speakChatTurn(userId: string, conversationId: string, turnSeq: number): Promise<ApiResponse<SpokenReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/speakChatTurn`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "conversationId": conversationId, "turnSeq": turnSeq}),
            cache: 'no-store'
        });
        const data: ApiResponse<SpokenReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async speakChatThinking(userId: string, turnKey: string): Promise<ApiResponse<SpokenThinkingResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/speakChatThinking`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<SpokenThinkingResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listVoices(userId: string, search: string | null, pageToken: string | null): Promise<ApiResponse<VoiceListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listVoices`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "search": search, "pageToken": pageToken}),
            cache: 'no-store'
        });
        const data: ApiResponse<VoiceListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async generateProductPhotos(userId: string, imageUrls: string[], count: number, notes: string | null): Promise<ApiResponse<GeneratedPhotosResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/generateProductPhotos`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "imageUrls": imageUrls, "count": count, "notes": notes}),
            cache: 'no-store'
        });
        const data: ApiResponse<GeneratedPhotosResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async makeProductPhotoChoices(userId: string, productId: string, imageUrl: string): Promise<ApiResponse<ProductPhotoChoicesResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/makeProductPhotoChoices`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "productId": productId, "imageUrl": imageUrl}),
            cache: 'no-store'
        });
        const data: ApiResponse<ProductPhotoChoicesResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async setProductPhoto(userId: string, productId: string, photoId: string | null): Promise<ApiResponse<AdminProductRow>> {
        try {
        const res = await fetch(`${this.apiUrl}/setProductPhoto`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "productId": productId, "photoId": photoId}),
            cache: 'no-store'
        });
        const data: ApiResponse<AdminProductRow> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }
}

export interface RouteResponseModels {
    loginWithGoogle: UserResponse;
    devGetUserList: DevUserListResponse;
    devLogin: UserResponse;
    getRoles: RoleListResponse;
    getUserRoleTypes: NoContentResponse;
    getUserStore: MyStoreResponse;
    updateMyStoreTheme: StoreThemeResponse;
    createUserStore: StoreResponse;
    updateMyStoreDetails: StoreResponse;
    suggestStoreAddress: AddressSuggestionResponse;
    setUserStoreLocation: StoreResponse;
    setMyDeliveryZones: DeliveryZonesResponse;
    resetMyStore: MyStoreResponse;
    startGuestSession: CustomerSessionResponse;
    verifyCustomerPhone: PhoneVerifiedResponse;
    listStoreProducts: ProductListResponse;
    addStoreProduct: AdminProductRow;
    addStoreProducts: AdminProductListResponse;
    updateStoreProduct: AdminProductRow;
    listMyStoreProducts: AdminProductListResponse;
    getStoreOrders: StoreOrderListResponse;
    getMyOrders: MyOrderListResponse;
    createOrder: CreatedOrderResponse;
    payOrder: MyOrderRow;
    completeOrder: StoreOrderRow;
    cancelStoreOrder: StoreOrderRow;
    cancelMyOrder: MyOrderRow;
    requestReturn: MyReturnRow;
    getMyReturns: MyReturnListResponse;
    cancelMyReturn: MyReturnRow;
    getStoreReturns: StoreReturnListResponse;
    approveReturn: StoreReturnRow;
    rejectReturn: StoreReturnRow;
    receiveReturn: StoreReturnRow;
    getMyStorePolicy: StorePolicyResponse;
    getStorePolicy: StorePolicyResponse;
    updateMyStorePolicy: StorePolicyResponse;
    sendAdminChatMessage: ChatReplyResponse;
    sendStoreChatMessage: ChatReplyResponse;
    getChatThinking: ChatThinkingResponse;
    listMyConversations: ConversationListResponse;
    getConversationTurns: ConversationTurnsResponse;
    sendAdminVoiceMessage: VoiceReplyResponse;
    sendStoreVoiceMessage: VoiceReplyResponse;
    speakChatTurn: SpokenReplyResponse;
    speakChatThinking: SpokenThinkingResponse;
    listVoices: VoiceListResponse;
    generateProductPhotos: GeneratedPhotosResponse;
    makeProductPhotoChoices: ProductPhotoChoicesResponse;
    setProductPhoto: AdminProductRow;
}

export interface RouteRequestBodies {
    loginWithGoogle: { firebaseUserObject: FirebaseUserObject };
    devGetUserList: Record<string, never>;
    devLogin: { userId: string };
    getRoles: { userId: string };
    getUserRoleTypes: { userId: string };
    getUserStore: { userId: string };
    updateMyStoreTheme: { userId: string; photoStyle: string | null; primaryColor: string | null; secondaryColor: string | null; voiceId: string | null };
    createUserStore: { userId: string; name: string; description: string; contactNumber: string; primaryColor: string; secondaryColor: string; currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED'; openingHours: string | null; address: string | null };
    updateMyStoreDetails: { userId: string; name: string | null; description: string | null; contactNumber: string | null; address: string | null; openingHours: string | null };
    suggestStoreAddress: { userId: string };
    setUserStoreLocation: { userId: string; latitude: number; longitude: number };
    setMyDeliveryZones: { userId: string; zones: NewDeliveryZone[] };
    resetMyStore: { userId: string; confirmName: string };
    startGuestSession: { idToken: string; storeSlug: string };
    verifyCustomerPhone: { userId: string; idToken: string };
    listStoreProducts: { userId: string; storeId: string; search: string | null; limit: number };
    addStoreProduct: { userId: string; name: string; price: number; inventory: number; description: string | null; isAvailable: boolean };
    addStoreProducts: { userId: string; products: NewProduct[] };
    updateStoreProduct: { userId: string; productId: string; name: string | null; description: string | null; price: number | null; inventory: number | null; isAvailable: boolean | null };
    listMyStoreProducts: { userId: string; search: string | null; limit: number };
    getStoreOrders: { userId: string; status: string | null; period: string | null; startDate: string | null; endDate: string | null; limit: number };
    getMyOrders: { userId: string; storeId: string; status: string | null; limit: number };
    createOrder: { userId: string; storeId: string; items: NewOrderItem[]; fulfillment: 'pickup' | 'delivery' | null; delivery: NewOrderDelivery | null };
    payOrder: { userId: string; storeId: string; orderId: string; paymentMethod: string };
    completeOrder: { userId: string; orderId: string };
    cancelStoreOrder: { userId: string; orderId: string };
    cancelMyOrder: { userId: string; storeId: string; orderId: string };
    requestReturn: { userId: string; storeId: string; orderId: string; items: NewReturnItem[]; reason: string; note: string | null };
    getMyReturns: { userId: string; storeId: string; limit: number | null };
    cancelMyReturn: { userId: string; storeId: string; returnId: string };
    getStoreReturns: { userId: string; status: string | null; limit: number | null };
    approveReturn: { userId: string; returnId: string };
    rejectReturn: { userId: string; returnId: string; reason: string };
    receiveReturn: { userId: string; returnId: string; restock: boolean };
    getMyStorePolicy: { userId: string };
    getStorePolicy: { userId: string; storeId: string };
    updateMyStorePolicy: { userId: string; acceptsReturns: boolean | null; returnWindowDays: number | null; returnConditions: string | null; nonReturnableItems: string | null; refundMethod: string | null; returnShippingPaidBy: string | null; defectReportDays: number | null; deliveryOptions: string[] | null; deliveryAreas: string | null; handoverDays: number | null; acceptedPaymentMethods: string[] | null; paidOrderCancellation: string | null; paidOrderCancellationHours: number | null; additionalTerms: string | null; clear: string[] | null };
    sendAdminChatMessage: { userId: string; message: string | null; conversationId: string | null; form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | null; attachments: string[] | null; turnKey: string | null; speak: boolean | null };
    sendStoreChatMessage: { userId: string; storeId: string; message: string | null; conversationId: string | null; form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | null; attachments: string[] | null; turnKey: string | null; speak: boolean | null };
    getChatThinking: { userId: string; turnKey: string };
    listMyConversations: { userId: string; storeId: string | null; before: string | null; limit: number };
    getConversationTurns: { userId: string; conversationId: string; beforeSeq: number | null; limit: number };
    sendAdminVoiceMessage: { userId: string; audio: string; conversationId: string | null; turnKey: string | null };
    sendStoreVoiceMessage: { userId: string; storeId: string; audio: string; conversationId: string | null; turnKey: string | null };
    speakChatTurn: { userId: string; conversationId: string; turnSeq: number };
    speakChatThinking: { userId: string; turnKey: string };
    listVoices: { userId: string; search: string | null; pageToken: string | null };
    generateProductPhotos: { userId: string; imageUrls: string[]; count: number; notes: string | null };
    makeProductPhotoChoices: { userId: string; productId: string; imageUrl: string };
    setProductPhoto: { userId: string; productId: string; photoId: string | null };
}

export type PublicRoute =
    | 'devGetUserList';

export default ServerRequests;
