import Server from "./Server";
import type { ApiResponse } from "./ApiResponse";
import type { AdminProductListResponse } from '../schemas/AdminProductListResponseSchema';
import type { AdminProductRow } from '../schemas/AdminProductRowSchema';
import type { ChatReplyResponse } from '../schemas/ChatReplyResponseSchema';
import type { ConversationListResponse } from '../schemas/ConversationListResponseSchema';
import type { ConversationTurnsResponse } from '../schemas/ConversationTurnsResponseSchema';
import type { CreatedOrderResponse } from '../schemas/CreatedOrderResponseSchema';
import type { CustomerSessionResponse } from '../schemas/CustomerSessionResponseSchema';
import type { DevUserListResponse } from '../schemas/DevUserListResponseSchema';
import type { FirebaseUserObject } from '../schemas/FirebaseUserObjectSchema';
import type { GeneratedPhotosResponse } from '../schemas/GeneratedPhotosResponseSchema';
import type { MyOrderListResponse } from '../schemas/MyOrderListResponseSchema';
import type { MyOrderRow } from '../schemas/MyOrderRowSchema';
import type { MyStoreResponse } from '../schemas/MyStoreResponseSchema';
import type { NewOrderItem } from '../schemas/NewOrderItemSchema';
import type { NewProduct } from '../schemas/NewProductSchema';
import type { NoContentResponse } from '../schemas/NoContentResponseSchema';
import type { PhoneVerifiedResponse } from '../schemas/PhoneVerifiedResponseSchema';
import type { ProductListResponse } from '../schemas/ProductListResponseSchema';
import type { RoleListResponse } from '../schemas/RoleListResponseSchema';
import type { StoreOrderListResponse } from '../schemas/StoreOrderListResponseSchema';
import type { StoreOrderRow } from '../schemas/StoreOrderRowSchema';
import type { StorePolicyResponse } from '../schemas/StorePolicyResponseSchema';
import type { StoreResponse } from '../schemas/StoreResponseSchema';
import type { StoreThemeResponse } from '../schemas/StoreThemeResponseSchema';
import type { UserResponse } from '../schemas/UserResponseSchema';

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

    async updateMyStoreTheme(userId: string, photoStyle: string | null, primaryColor: string | null, secondaryColor: string | null): Promise<ApiResponse<StoreThemeResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateMyStoreTheme`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "photoStyle": photoStyle, "primaryColor": primaryColor, "secondaryColor": secondaryColor}),
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

    async createUserStore(userId: string, name: string, description: string, contactNumber: string, address: string, currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED', openingHours: string | null): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createUserStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "name": name, "description": description, "contactNumber": contactNumber, "address": address, "currency": currency, "openingHours": openingHours}),
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

    async createOrder(userId: string, storeId: string, items: NewOrderItem[]): Promise<ApiResponse<CreatedOrderResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createOrder`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "items": items}),
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

    async updateMyStorePolicy(userId: string, acceptsReturns: boolean | null, returnWindowDays: number | null, returnConditions: string | null, nonReturnableItems: string | null, refundMethod: string | null, returnShippingPaidBy: string | null, defectReportDays: number | null, deliveryOptions: string[] | null, deliveryAreas: string | null, deliveryFee: number | null, freeDeliveryMinimum: number | null, handoverDays: number | null, acceptedPaymentMethods: string[] | null, paidOrderCancellation: string | null, sendsMarketingMessages: boolean | null, additionalTerms: string | null, clear: string[] | null): Promise<ApiResponse<StorePolicyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/updateMyStorePolicy`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "acceptsReturns": acceptsReturns, "returnWindowDays": returnWindowDays, "returnConditions": returnConditions, "nonReturnableItems": nonReturnableItems, "refundMethod": refundMethod, "returnShippingPaidBy": returnShippingPaidBy, "defectReportDays": defectReportDays, "deliveryOptions": deliveryOptions, "deliveryAreas": deliveryAreas, "deliveryFee": deliveryFee, "freeDeliveryMinimum": freeDeliveryMinimum, "handoverDays": handoverDays, "acceptedPaymentMethods": acceptedPaymentMethods, "paidOrderCancellation": paidOrderCancellation, "sendsMarketingMessages": sendsMarketingMessages, "additionalTerms": additionalTerms, "clear": clear}),
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

    async sendAdminChatMessage(userId: string, message: string, conversationId: string | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendAdminChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "message": message, "conversationId": conversationId}),
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

    async sendStoreChatMessage(userId: string, storeId: string, message: string, conversationId: string | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendStoreChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "message": message, "conversationId": conversationId}),
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
    setUserStoreLocation: StoreResponse;
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
    getMyStorePolicy: StorePolicyResponse;
    getStorePolicy: StorePolicyResponse;
    updateMyStorePolicy: StorePolicyResponse;
    sendAdminChatMessage: ChatReplyResponse;
    sendStoreChatMessage: ChatReplyResponse;
    listMyConversations: ConversationListResponse;
    getConversationTurns: ConversationTurnsResponse;
    generateProductPhotos: GeneratedPhotosResponse;
}

export interface RouteRequestBodies {
    loginWithGoogle: { firebaseUserObject: FirebaseUserObject };
    devGetUserList: Record<string, never>;
    devLogin: { userId: string };
    getRoles: { userId: string };
    getUserRoleTypes: { userId: string };
    getUserStore: { userId: string };
    updateMyStoreTheme: { userId: string; photoStyle: string | null; primaryColor: string | null; secondaryColor: string | null };
    createUserStore: { userId: string; name: string; description: string; contactNumber: string; address: string; currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED'; openingHours: string | null };
    setUserStoreLocation: { userId: string; latitude: number; longitude: number };
    startGuestSession: { idToken: string; storeSlug: string };
    verifyCustomerPhone: { userId: string; idToken: string };
    listStoreProducts: { userId: string; storeId: string; search: string | null; limit: number };
    addStoreProduct: { userId: string; name: string; price: number; inventory: number; description: string | null; isAvailable: boolean };
    addStoreProducts: { userId: string; products: NewProduct[] };
    updateStoreProduct: { userId: string; productId: string; name: string | null; description: string | null; price: number | null; inventory: number | null; isAvailable: boolean | null };
    listMyStoreProducts: { userId: string; search: string | null; limit: number };
    getStoreOrders: { userId: string; status: string | null; period: string | null; startDate: string | null; endDate: string | null; limit: number };
    getMyOrders: { userId: string; storeId: string; status: string | null; limit: number };
    createOrder: { userId: string; storeId: string; items: NewOrderItem[] };
    payOrder: { userId: string; storeId: string; orderId: string; paymentMethod: string };
    completeOrder: { userId: string; orderId: string };
    cancelStoreOrder: { userId: string; orderId: string };
    cancelMyOrder: { userId: string; storeId: string; orderId: string };
    getMyStorePolicy: { userId: string };
    getStorePolicy: { userId: string; storeId: string };
    updateMyStorePolicy: { userId: string; acceptsReturns: boolean | null; returnWindowDays: number | null; returnConditions: string | null; nonReturnableItems: string | null; refundMethod: string | null; returnShippingPaidBy: string | null; defectReportDays: number | null; deliveryOptions: string[] | null; deliveryAreas: string | null; deliveryFee: number | null; freeDeliveryMinimum: number | null; handoverDays: number | null; acceptedPaymentMethods: string[] | null; paidOrderCancellation: string | null; sendsMarketingMessages: boolean | null; additionalTerms: string | null; clear: string[] | null };
    sendAdminChatMessage: { userId: string; message: string; conversationId: string | null };
    sendStoreChatMessage: { userId: string; storeId: string; message: string; conversationId: string | null };
    listMyConversations: { userId: string; storeId: string | null; before: string | null; limit: number };
    getConversationTurns: { userId: string; conversationId: string; beforeSeq: number | null; limit: number };
    generateProductPhotos: { userId: string; imageUrls: string[]; count: number; notes: string | null };
}

export type PublicRoute =
    | 'devGetUserList';

export default ServerRequests;
